"""Churn-risk scoring for customer accounts.

Features are aggregate signals (recency / frequency / monetary / skips / payment
failures). If a trained LightGBM artifact is present it is used; otherwise a
transparent, deterministic weighted-logistic heuristic scores the account. Either
way the score is 0..1 and bucketed low/medium/high. CPU-bound model inference is
offloaded with asyncio.to_thread. No PII — only counts/dates/amounts.
"""
import asyncio
import datetime as dt
import math
import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.churn import ChurnScore
from app.models.order import Order
from app.models.payment import Payment
from app.models.shop import CustomerAccount
from app.models.subscription import Subscription, SubscriptionItem, SubscriptionOverride

_MODEL_PATH = os.environ.get("CHURN_MODEL_PATH", "models/churn_lgbm.txt")
# Heuristic weights (documented): higher recency/failures/skips => higher churn risk.
_W = {
    "bias": -1.4,
    "days_since_last_order": 0.020,   # per day since last order
    "order_count": -0.18,             # more orders => lower risk
    "skip_count": 0.22,               # recent subscription skips
    "payment_failures": 0.5,          # failed payments
    "has_active_sub": -0.8,           # an active subscription is sticky
}
_HIGH, _MEDIUM = 0.66, 0.33


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def risk_bucket(score: float) -> str:
    if score >= _HIGH:
        return "high"
    if score >= _MEDIUM:
        return "medium"
    return "low"


async def extract_features(db: AsyncSession, account_id: int) -> dict:
    today = dt.datetime.now(dt.UTC).date()
    order_count = int(
        await db.scalar(select(func.count()).select_from(Order).where(Order.account_id == account_id)) or 0
    )
    last_order = await db.scalar(
        select(func.max(func.date(Order.created_at))).where(Order.account_id == account_id)
    )
    days_since_last_order = (today - last_order).days if last_order else 365
    payment_failures = int(
        await db.scalar(
            select(func.count()).select_from(Payment).where(
                Payment.account_id == account_id, Payment.status == "failed"
            )
        ) or 0
    )
    has_active_sub = bool(
        await db.scalar(
            select(func.count()).select_from(Subscription).where(
                Subscription.account_id == account_id, Subscription.status == "active"
            )
        )
    )
    # Recent subscription skips (override qty == 0) across the account's subs.
    skip_count = int(
        await db.scalar(
            select(func.count())
            .select_from(SubscriptionOverride)
            .join(SubscriptionItem, SubscriptionItem.id == SubscriptionOverride.subscription_item_id)
            .join(Subscription, Subscription.id == SubscriptionItem.subscription_id)
            .where(Subscription.account_id == account_id, SubscriptionOverride.override_qty == 0)
        ) or 0
    )
    return {
        "days_since_last_order": days_since_last_order,
        "order_count": order_count,
        "skip_count": skip_count,
        "payment_failures": payment_failures,
        "has_active_sub": int(has_active_sub),
    }


def _heuristic_score(f: dict) -> float:
    z = (
        _W["bias"]
        + _W["days_since_last_order"] * min(f["days_since_last_order"], 180)
        + _W["order_count"] * min(f["order_count"], 20)
        + _W["skip_count"] * min(f["skip_count"], 15)
        + _W["payment_failures"] * min(f["payment_failures"], 10)
        + _W["has_active_sub"] * f["has_active_sub"]
    )
    return _sigmoid(z)


def _lgbm_score(f: dict) -> float | None:
    """Score via a trained LightGBM model if the artifact exists; else None."""
    if not os.path.exists(_MODEL_PATH):
        return None
    try:
        import lightgbm as lgb  # lazy, optional

        booster = lgb.Booster(model_file=_MODEL_PATH)
        order = ["days_since_last_order", "order_count", "skip_count", "payment_failures", "has_active_sub"]
        pred = booster.predict([[float(f[k]) for k in order]])
        return float(pred[0])
    except Exception:
        return None


async def score_account(db: AsyncSession, account_id: int) -> dict:
    features = await extract_features(db, account_id)
    score = await asyncio.to_thread(_lgbm_score, features)
    if score is None:
        score = _heuristic_score(features)
    score = max(0.0, min(1.0, round(score, 3)))
    return {"account_id": account_id, "score": score, "risk": risk_bucket(score), "features": features}


async def score_all(db: AsyncSession) -> dict:
    account_ids = (await db.execute(select(CustomerAccount.id))).scalars().all()
    counts = {"low": 0, "medium": 0, "high": 0, "scored": 0}
    for aid in account_ids:
        result = await score_account(db, aid)
        existing = await db.scalar(select(ChurnScore).where(ChurnScore.account_id == aid).with_for_update())
        if existing is None:
            db.add(ChurnScore(
                account_id=aid, score=result["score"], risk=result["risk"], features=result["features"],
            ))
        else:
            existing.score = result["score"]
            existing.risk = result["risk"]
            existing.features = result["features"]
            existing.computed_at = func.now()
        counts[result["risk"]] += 1
        counts["scored"] += 1
    await db.commit()
    return counts
