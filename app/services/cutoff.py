"""The daily-cutoff batch: materialize active subscriptions into delivery_orders.

Idempotent + chunked: subscriptions are processed in batches with a commit per
chunk, so a huge run never holds one giant transaction. A *session-level* advisory
lock (held across commits, unlike an xact lock) serializes runs for a date; a
second concurrent run (cron + manual admin) sees the lock held and skips.

Per order we take a row lock and refuse to clobber anything already past
'generated'/'frozen' (i.e. assigned/dispatched/delivered).
"""
import datetime as dt

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import DeliveryOrder, DeliveryOrderItem, ScheduleRun
from app.models.shop import Shop
from app.models.subscription import Subscription
from app.services.pricing import resolve_tier_id, resolve_unit_price
from app.services.subscription import compute_plan

_LOCK_NS = 424242
CHUNK = 200


async def _materialize_one(db: AsyncSession, sub: Subscription, delivery_date: dt.date, run_id: int) -> bool:
    lines = await compute_plan(db, sub, delivery_date)
    if not lines:
        return False

    res = await db.execute(
        select(DeliveryOrder)
        .where(
            DeliveryOrder.shop_id == sub.shop_id,
            DeliveryOrder.delivery_date == delivery_date,
        )
        .with_for_update()
    )
    order = res.scalar_one_or_none()
    if order is not None and order.status not in ("generated", "frozen"):
        return False  # never clobber assigned/dispatched/delivered
    if order is None:
        order = DeliveryOrder(
            delivery_date=delivery_date,
            account_id=sub.account_id,
            shop_id=sub.shop_id,
            origin="subscription",
            schedule_run_id=run_id,
            status="generated",
        )
        db.add(order)
        await db.flush()
    else:
        order.schedule_run_id = run_id
        order.status = "generated"
        await db.execute(
            delete(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
        )

    shop = await db.get(Shop, sub.shop_id)
    tier_id = await resolve_tier_id(db, shop)
    subtotal = 0.0
    for line in lines:
        unit_price = await resolve_unit_price(db, line["product_id"], tier_id, line["qty"])
        line_total = round(unit_price * line["qty"], 2)
        subtotal += line_total
        db.add(
            DeliveryOrderItem(
                delivery_order_id=order.id,
                product_id=line["product_id"],
                qty_planned=line["qty"],
                unit_price=unit_price,
                line_total=line_total,
                status="planned",
            )
        )
    order.subtotal = subtotal
    order.total = subtotal
    order.amount_due = subtotal
    return True


async def run_cutoff(db: AsyncSession, delivery_date: dt.date) -> dict:
    lock_key = int(delivery_date.strftime("%Y%m%d"))
    got = await db.scalar(select(func.pg_try_advisory_lock(_LOCK_NS, lock_key)))
    if not got:
        return {
            "schedule_run_id": None,
            "delivery_date": delivery_date,
            "orders_generated": 0,
            "skipped": "locked",
        }
    try:
        run = ScheduleRun(for_delivery_date=delivery_date, status="generating")
        db.add(run)
        await db.flush()
        run_id = run.id
        await db.commit()  # persist the run row up front

        ids = (
            await db.execute(
                select(Subscription.id)
                .where(Subscription.status == "active")
                .order_by(Subscription.id)
            )
        ).scalars().all()

        count = 0
        for start in range(0, len(ids), CHUNK):
            chunk_ids = ids[start : start + CHUNK]
            subs = (
                await db.execute(select(Subscription).where(Subscription.id.in_(chunk_ids)))
            ).scalars().all()
            for sub in subs:
                if await _materialize_one(db, sub, delivery_date, run_id):
                    count += 1
            await db.commit()  # commit per chunk

        run = await db.get(ScheduleRun, run_id)
        run.status = "frozen"
        run.orders_generated = count
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        await db.commit()
        return {
            "schedule_run_id": run_id,
            "delivery_date": delivery_date,
            "orders_generated": count,
        }
    finally:
        await db.execute(select(func.pg_advisory_unlock(_LOCK_NS, lock_key)))
        await db.commit()
