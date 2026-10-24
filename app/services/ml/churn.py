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
import secrets

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.models.churn import ChurnScore
from app.models.order import Order
from app.models.payment import Payment
from app.models.shop import CustomerAccount
from app.models.subscription import Subscription, SubscriptionItem, SubscriptionOverride

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)
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


_booster_cache: tuple[float, object] | None = None  # (mtime, Booster) — reload on change


def _get_booster():
    """Load the LightGBM booster once and cache it, reloading only if the artifact's
    mtime changes (not once per account as before)."""
    global _booster_cache
    if not os.path.exists(_MODEL_PATH):
        return None
    mtime = os.path.getmtime(_MODEL_PATH)
    if _booster_cache is not None and _booster_cache[0] == mtime:
        return _booster_cache[1]
    import lightgbm as lgb  # lazy, optional

    booster = lgb.Booster(model_file=_MODEL_PATH)
    _booster_cache = (mtime, booster)
    return booster


def _lgbm_score(f: dict) -> float | None:
    """Score via a trained LightGBM model if the artifact exists; else None."""
    try:
        booster = _get_booster()
        if booster is None:
            return None
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


_LOCK_KEY = "churn:score_all:lock"


async def score_all(db: AsyncSession) -> dict:
    """Score every account. One run at a time (admin trigger vs nightly job): a Redis
    lock (fail-open), plus an idempotent UPSERT so even overlapping runs can't hit the
    UNIQUE(account_id) race (FOR UPDATE can't lock a row that doesn't exist yet) and lose
    a whole chunk; accounts are processed in id order so row locks are taken in one order."""
    counts = {"low": 0, "medium": 0, "high": 0, "scored": 0}
    token = secrets.token_hex(8)
    redis_ok = True
    try:
        # SET NX returns None (not False) when the key exists.
        got = await redis_client.set(_LOCK_KEY, token, nx=True, ex=3600)
    except Exception:
        got, redis_ok = None, False  # Redis down: proceed (the upsert keeps it correct)
    if redis_ok and not got:
        return counts  # another run is in progress
    try:
        account_ids = (
            await db.execute(select(CustomerAccount.id).order_by(CustomerAccount.id))
        ).scalars().all()
        # Commit in chunks so a large run never holds row locks on every ChurnScore in one
        # giant transaction (matches the cutoff's chunking).
        for i, aid in enumerate(account_ids, start=1):
            result = await score_account(db, aid)
            stmt = pg_insert(ChurnScore).values(
                account_id=aid, score=result["score"], risk=result["risk"],
                features=result["features"],
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[ChurnScore.account_id],
                    set_={
                        "score": stmt.excluded.score, "risk": stmt.excluded.risk,
                        "features": stmt.excluded.features, "computed_at": func.now(),
                    },
                )
            )
            counts[result["risk"]] += 1
            counts["scored"] += 1
            if i % 200 == 0:
                await db.commit()
        await db.commit()
        return counts
    finally:
        if got:
            try:
                # Atomic compare-and-delete: never release a lock another run re-took.
                await redis_client.eval(_RELEASE_LUA, 1, _LOCK_KEY, token)
            except Exception:
                pass
