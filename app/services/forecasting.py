"""Next-day demand forecasting for the production plan.

Strategy (per the drs-ai skill): the committed standing-order quantities for a date
are *known* (we materialize them at cutoff), so we only forecast the *variable*
part — ad-hoc one-time orders — and add it to the committed base.

This is a dependency-free baseline (EWMA for regular demand, Croston-style for
intermittent SKUs, plus a service-level safety buffer). It implements the interface
the separate ML service will later back with statsforecast (AutoARIMA/ETS/Croston).
Runs on our own infra on aggregate quantities only — no PII leaves the box.
"""
import datetime as dt
import statistics

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.subscription import Subscription
from app.services.subscription import compute_plan

# ~90% cycle service level (one-sided normal quantile) for the safety buffer.
Z_SERVICE = 1.28
DEFAULT_HISTORY_DAYS = 28


def forecast_variable(daily_qtys: list[float]) -> dict:
    """Point forecast + safety buffer for one product's variable daily demand."""
    if not daily_qtys or sum(daily_qtys) == 0:
        return {"point": 0.0, "safety": 0.0, "method": "none"}

    nonzero = [q for q in daily_qtys if q > 0]
    intermittent = len(nonzero) < len(daily_qtys) * 0.5
    if intermittent:
        # Croston-style: average demand size * probability of a demand day.
        prob = len(nonzero) / len(daily_qtys)
        point = prob * statistics.fmean(nonzero)
        method = "croston"
    else:
        alpha = 0.4  # exponentially weighted moving average
        s = daily_qtys[0]
        for q in daily_qtys[1:]:
            s = alpha * q + (1 - alpha) * s
        point = s
        method = "ewma"

    sd = statistics.pstdev(daily_qtys) if len(daily_qtys) > 1 else 0.0
    return {"point": round(point, 2), "safety": round(Z_SERVICE * sd, 2), "method": method}


async def _variable_history(
    db: AsyncSession, since: dt.date
) -> dict[int, dict[dt.date, float]]:
    """One-time order quantities per product per day (the variable demand)."""
    day = func.date(Order.created_at)
    rows = await db.execute(
        select(day, OrderItem.product_id, func.sum(OrderItem.qty))
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(Order.order_type == "one_time", func.date(Order.created_at) >= since)
        .group_by(day, OrderItem.product_id)
    )
    hist: dict[int, dict[dt.date, float]] = {}
    for d, pid, qty in rows.all():
        parsed = d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))
        hist.setdefault(pid, {})[parsed] = float(qty or 0)
    return hist


async def _committed(db: AsyncSession, for_date: dt.date) -> dict[int, float]:
    """Known standing-order demand for the date, summed per product."""
    subs = (
        await db.execute(select(Subscription).where(Subscription.status == "active"))
    ).scalars().all()
    committed: dict[int, float] = {}
    for sub in subs:
        for line in await compute_plan(db, sub, for_date):
            committed[line["product_id"]] = committed.get(line["product_id"], 0.0) + float(
                line["qty"]
            )
    return committed


async def production_plan(
    db: AsyncSession, for_date: dt.date, history_days: int = DEFAULT_HISTORY_DAYS
) -> list[dict]:
    """Per-product production recommendation = committed + forecast(variable) + safety."""
    today = dt.datetime.now(dt.UTC).date()
    since = today - dt.timedelta(days=history_days)
    committed = await _committed(db, for_date)
    history = await _variable_history(db, since)

    product_ids = set(committed) | set(history)
    names = {
        p.id: p.name
        for p in (
            await db.execute(select(Product).where(Product.id.in_(product_ids)))
        ).scalars().all()
    } if product_ids else {}

    # Build a dense daily series (missing days = 0 demand) per product.
    dates = [since + dt.timedelta(days=i) for i in range((today - since).days + 1)]

    plan = []
    for pid in sorted(product_ids):
        series = [history.get(pid, {}).get(d, 0.0) for d in dates]
        fc = forecast_variable(series)
        committed_qty = round(committed.get(pid, 0.0), 2)
        variable = fc["point"] + fc["safety"]
        plan.append(
            {
                "product_id": pid,
                "product_name": names.get(pid),
                "committed_qty": committed_qty,
                "forecast_variable_qty": round(fc["point"], 2),
                "safety_qty": fc["safety"],
                "recommended_production": round(committed_qty + variable, 2),
                "method": fc["method"],
            }
        )
    return plan
