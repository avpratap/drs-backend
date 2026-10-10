"""The daily-cutoff batch: materialize active subscriptions into delivery_orders.

Idempotent: re-running for the same date replaces each shop's items (keyed by the
UNIQUE(shop_id, delivery_date) constraint). In production this runs as a scheduled
background job (ARQ -> Celery Beat) and chunks work per shop/route.
"""
import datetime as dt

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import DeliveryOrder, DeliveryOrderItem, ScheduleRun
from app.models.shop import Shop
from app.models.subscription import Subscription
from app.services.pricing import resolve_tier_id, resolve_unit_price
from app.services.subscription import compute_plan


async def run_cutoff(db: AsyncSession, delivery_date: dt.date) -> dict:
    # Serialize cutoff runs for this date (cron + manual admin trigger can't race).
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :d)"),
        {"ns": 424242, "d": int(delivery_date.strftime("%Y%m%d"))},
    )

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
            select(DeliveryOrder)
            .where(
                DeliveryOrder.shop_id == sub.shop_id,
                DeliveryOrder.delivery_date == delivery_date,
            )
            .with_for_update()
        )
        order = res.scalar_one_or_none()
        # Never clobber an order that's already past generation (assigned/dispatched/delivered).
        if order is not None and order.status not in ("generated", "frozen"):
            continue
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
