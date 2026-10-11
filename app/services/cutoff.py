"""The daily-cutoff batch: materialize active subscriptions into delivery_orders.

Idempotent + chunked: subscriptions are processed in batches with a commit per
chunk, so a huge run never holds one giant transaction. A *session-level* advisory
lock (held across commits, unlike an xact lock) serializes runs for a date; a
second concurrent run (cron + manual admin) sees the lock held and skips.

Per order we take a row lock and refuse to clobber anything already past
'generated'/'frozen' (i.e. assigned/dispatched/delivered).
"""
import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import DeliveryOrder, DeliveryOrderItem, ScheduleRun
from app.models.shop import CustomerAccount, Shop
from app.models.subscription import Subscription
from app.models.user import User
from app.services import notifications as notify
from app.services.pricing import PriceBook, load_price_book
from app.services.subscription import compute_plan

_LOCK_NS = 424242
CHUNK = 200


@dataclass
class _Ctx:
    """Reference data preloaded once per run so per-subscription work does no
    shop/account/user/price lookups (eliminates the cutoff's N+1)."""

    shops: dict[int, Shop]
    accounts: dict[int, CustomerAccount]
    owners: dict[int, User | None]  # keyed by account_id
    book: PriceBook


async def _load_ctx(db: AsyncSession) -> _Ctx:
    rows = (
        await db.execute(
            select(Subscription.shop_id, Subscription.account_id).where(
                Subscription.status == "active"
            )
        )
    ).all()
    shop_ids = {r[0] for r in rows}
    account_ids = {r[1] for r in rows}
    shops = (
        {s.id: s for s in (await db.execute(select(Shop).where(Shop.id.in_(shop_ids)))).scalars()}
        if shop_ids
        else {}
    )
    accounts = (
        {
            a.id: a
            for a in (
                await db.execute(
                    select(CustomerAccount).where(CustomerAccount.id.in_(account_ids))
                )
            ).scalars()
        }
        if account_ids
        else {}
    )
    owner_ids = {a.owner_user_id for a in accounts.values()}
    users = (
        {u.id: u for u in (await db.execute(select(User).where(User.id.in_(owner_ids)))).scalars()}
        if owner_ids
        else {}
    )
    owners = {a.id: users.get(a.owner_user_id) for a in accounts.values()}
    return _Ctx(shops, accounts, owners, await load_price_book(db))


async def _materialize_one(
    db: AsyncSession, sub: Subscription, delivery_date: dt.date, run_id: int, ctx: _Ctx
) -> bool:
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

    shop = ctx.shops.get(sub.shop_id)
    account = ctx.accounts.get(sub.account_id)
    tier_id = ctx.book.tier_for(shop, account) if shop is not None else None
    subtotal = 0.0
    for line in lines:
        unit_price = ctx.book.unit_price(line["product_id"], tier_id, line["qty"])
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

    # Tell the account owner tomorrow's order is set (idempotent per delivery_order;
    # a cutoff re-run hits the dedupe savepoint and skips silently).
    owner = ctx.owners.get(sub.account_id)
    if owner is not None:
        await notify.emit_event(
            db,
            "order.generated",
            owner,
            {"date": str(delivery_date), "summary": f"{len(lines)} item(s)", "total": subtotal},
            account_id=sub.account_id,
            dedupe_key=f"order.generated:{order.id}",
        )
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

        ctx = await _load_ctx(db)  # reference data preloaded once (no per-sub N+1)
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
                if await _materialize_one(db, sub, delivery_date, run_id, ctx):
                    count += 1
            await db.commit()  # commit per chunk

        run = await db.get(ScheduleRun, run_id)
        run.status = "frozen"
        run.orders_generated = count
        run.finished_at = dt.datetime.now(dt.UTC)
        await db.commit()
        return {
            "schedule_run_id": run_id,
            "delivery_date": delivery_date,
            "orders_generated": count,
        }
    finally:
        await db.execute(select(func.pg_advisory_unlock(_LOCK_NS, lock_key)))
        await db.commit()
