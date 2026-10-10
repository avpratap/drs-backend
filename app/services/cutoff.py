"""The daily-cutoff batch: materialize active subscriptions into delivery_orders.

Idempotent: re-running for the same date replaces each shop's items (keyed by the
UNIQUE(shop_id, delivery_date) constraint). In production this runs as a scheduled
background job (ARQ -> Celery Beat) and chunks work per shop/route.
"""
import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import DeliveryOrder, DeliveryOrderItem, ScheduleRun
from app.models.subscription import Subscription
from app.services.subscription import compute_plan


async def run_cutoff(db: AsyncSession, delivery_date: dt.date) -> dict:
    run = ScheduleRun(for_delivery_date=delivery_date, status="generating")
    db.add(run)
    await db.flush()

    res = await db.execute(select(Subscription).where(Subscription.status == "active"))
    subs = res.scalars().all()

    count = 0
    for sub in subs:
        lines = await compute_plan(db, sub, delivery_date)
        if not lines:
            continue

        res = await db.execute(
            select(DeliveryOrder).where(
                DeliveryOrder.shop_id == sub.shop_id,
                DeliveryOrder.delivery_date == delivery_date,
            )
        )
        order = res.scalar_one_or_none()
        if order is None:
            order = DeliveryOrder(
                delivery_date=delivery_date,
                account_id=sub.account_id,
                shop_id=sub.shop_id,
                origin="subscription",
                schedule_run_id=run.id,
                status="generated",
            )
            db.add(order)
            await db.flush()
        else:
            order.schedule_run_id = run.id
            order.status = "generated"
            await db.execute(
                delete(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
            )

        for line in lines:
            db.add(
                DeliveryOrderItem(
                    delivery_order_id=order.id,
                    product_id=line["product_id"],
                    qty_planned=line["qty"],
                    status="planned",
                )
            )
        count += 1

    run.status = "frozen"
    run.orders_generated = count
    run.finished_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    return {
        "schedule_run_id": run.id,
        "delivery_date": delivery_date,
        "orders_generated": count,
    }
