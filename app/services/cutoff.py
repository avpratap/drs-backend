"""The daily-cutoff batch: materialize active subscriptions into delivery_orders.

Idempotent + chunked: subscriptions are processed in batches with a commit per
chunk, so a huge run never holds one giant transaction. A *session-level* advisory
lock (held across commits, unlike an xact lock) serializes runs for a date; a
second concurrent run (cron + manual admin) sees the lock held and skips.

Per order we take a row lock and refuse to clobber anything already past
'generated'/'frozen' (i.e. assigned/dispatched/delivered).
"""
import datetime as dt
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.models.billing import Wallet
from app.models.delivery import DeliveryOrder, DeliveryOrderItem, ScheduleRun
from app.models.shop import CustomerAccount, Shop
from app.models.subscription import Subscription
from app.models.user import User
from app.services import notifications as notify
from app.services.pricing import PriceBook, load_price_book
from app.services.subscription import compute_plan

CHUNK = 200
# Redis lock TTL. A real cutoff finishes in well under this; the key auto-expires so a
# crashed run can never deadlock a date forever (a pg session-advisory lock could not be
# held reliably across the per-chunk commits / connection-pool churn).
_LOCK_TTL = 3600

log = logging.getLogger("drs.cutoff")


@dataclass
class _Ctx:
    """Reference data preloaded once per run so per-subscription work does no
    shop/account/user/price lookups (eliminates the cutoff's N+1)."""

    shops: dict[int, Shop]
    accounts: dict[int, CustomerAccount]
    owners: dict[int, User | None]  # keyed by account_id
    wallets: dict[int, float]  # account_id -> current wallet balance (for prepaid gate)
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
    # Preload wallet balances for the retail prepaid accounts so the funds gate below
    # does no per-subscription lookup (keeps the cutoff N+1-free).
    prepaid_ids = {
        a.id
        for a in accounts.values()
        if a.account_type == "retail" and a.billing_mode == "prepaid_wallet"
    }
    wallets: dict[int, float] = {}
    if prepaid_ids:
        wrows = (
            await db.execute(
                select(Wallet.account_id, Wallet.balance).where(Wallet.account_id.in_(prepaid_ids))
            )
        ).all()
        wallets = {aid: float(bal) for aid, bal in wrows}
    return _Ctx(shops, accounts, owners, wallets, await load_price_book(db))


async def _materialize_one(
    db: AsyncSession, sub: Subscription, delivery_date: dt.date, run_id: int, ctx: _Ctx
) -> bool:
    lines = await compute_plan(db, sub, delivery_date)
    if not lines:
        return False

    shop = ctx.shops.get(sub.shop_id)
    account = ctx.accounts.get(sub.account_id)
    tier_id = ctx.book.tier_for(shop, account) if shop is not None else None

    # Price the plan first (so the prepaid funds gate can run before we touch the order).
    priced: list[tuple[int, float, float, float]] = []  # (product_id, qty, unit_price, line_total)
    subtotal = 0.0
    for line in lines:
        unit_price = ctx.book.unit_price(line["product_id"], tier_id, line["qty"])
        if unit_price is None:
            # Misconfiguration: a subscribed product has no price for this tier. Skip the
            # line (never materialize a silent ₹0 charge) and log loudly for ops.
            log.warning(
                "cutoff: no price for product %s (tier %s) on sub %s — line skipped",
                line["product_id"], tier_id, sub.id,
            )
            continue
        line_total = round(unit_price * line["qty"], 2)
        subtotal += line_total
        priced.append((line["product_id"], line["qty"], unit_price, line_total))
    if not priced:
        return False  # nothing priced -> nothing to deliver

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

    # Retail prepaid funds gate: never deliver a prepaid retail account into the negative.
    # If the wallet can't cover tomorrow's order, skip it, drop any stale pending order
    # from a prior run, and nudge the customer to top up.
    if (
        account is not None
        and account.account_type == "retail"
        and account.billing_mode == "prepaid_wallet"
        and ctx.wallets.get(sub.account_id, 0.0) < subtotal
    ):
        if order is not None:
            await db.execute(
                delete(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
            )
            await db.delete(order)
        owner = ctx.owners.get(sub.account_id)
        if owner is not None:
            await notify.emit_event(
                db, "wallet.low", owner,
                {"balance": round(ctx.wallets.get(sub.account_id, 0.0), 2), "needed": subtotal},
                account_id=sub.account_id,
                dedupe_key=f"cutoff.skipped_lowbalance:{sub.id}:{delivery_date}",
            )
        log.info(
            "cutoff: sub %s skipped — prepaid wallet %.2f < order %.2f",
            sub.id, ctx.wallets.get(sub.account_id, 0.0), subtotal,
        )
        return False

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

    for product_id, qty, unit_price, line_total in priced:
        db.add(
            DeliveryOrderItem(
                delivery_order_id=order.id,
                product_id=product_id,
                qty_planned=qty,
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
    # Redis lock keyed by the delivery date. Unlike a pg advisory lock, it survives the
    # per-chunk commits and connection-pool churn below. Fail-open if Redis is down — the
    # cutoff is critical and UNIQUE(shop_id, delivery_date) + row locks already prevent
    # data corruption from a concurrent run; the lock only avoids duplicated work.
    lock_key = f"cutoff:lock:{delivery_date.isoformat()}"
    token = secrets.token_hex(8)
    lock_acquired: bool | None = None
    try:
        lock_acquired = bool(await redis_client.set(lock_key, token, nx=True, ex=_LOCK_TTL))
    except Exception:
        log.warning("cutoff: redis lock unavailable for %s, proceeding without it", delivery_date)
        lock_acquired = None
    if lock_acquired is False:
        return {
            "schedule_run_id": None,
            "delivery_date": delivery_date,
            "orders_generated": 0,
            "skipped": "locked",
        }
    run_id = None
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
                # Isolate each subscription in a savepoint: one bad sub (data/price
                # error) is logged and skipped instead of aborting the whole run.
                try:
                    async with db.begin_nested():
                        if await _materialize_one(db, sub, delivery_date, run_id, ctx):
                            count += 1
                except Exception:
                    log.exception("cutoff: subscription %s failed — skipped", sub.id)
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
    except Exception:
        # Never leave the run stuck 'generating'. Mark it terminal for operability.
        await db.rollback()
        if run_id is not None:
            try:
                run = await db.get(ScheduleRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.finished_at = dt.datetime.now(dt.UTC)
                    await db.commit()
            except Exception:
                await db.rollback()
        raise
    finally:
        # Clear any aborted-transaction state before touching the DB again.
        try:
            await db.rollback()
        except Exception:
            pass
        # Release the Redis lock only if we still own it (compare token so we never
        # delete a lock a later run acquired after ours expired).
        if lock_acquired:
            try:
                if await redis_client.get(lock_key) == token:
                    await redis_client.delete(lock_key)
            except Exception:
                pass
