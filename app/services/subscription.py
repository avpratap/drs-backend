"""Subscription plan computation + the server-enforced next-day cutoff."""
import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.subscription import Subscription, SubscriptionItem, SubscriptionOverride

IST = ZoneInfo(settings.TIMEZONE)


def today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


MAX_FUTURE_OVERRIDES = 400  # per item: bounds the rows a script could create


def assert_editable(for_date: dt.date) -> None:
    """You may edit tomorrow (before cutoff) or any later date. Never today/past, never
    tomorrow once the cutoff hour has passed."""
    now = dt.datetime.now(IST)
    tomorrow = now.date() + dt.timedelta(days=1)
    if for_date < tomorrow:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "You can only modify tomorrow or a later date"
        )
    if for_date == tomorrow and now.hour >= settings.CUTOFF_HOUR:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cutoff passed (after {settings.CUTOFF_HOUR}:00) — tomorrow's order is frozen",
        )


def _dow_match(item: SubscriptionItem, d: dt.date) -> bool:
    if not item.days_of_week:
        return True
    days = {int(x) for x in item.days_of_week.split(",") if x.strip()}
    return d.isoweekday() in days


def _matches(item: SubscriptionItem, sub: Subscription, d: dt.date) -> bool:
    if item.frequency == "daily":
        return True
    if item.frequency == "weekly":
        return _dow_match(item, d)
    if item.frequency == "alternate":
        n = item.interval_days or 2
        return ((d - sub.start_date).days % n) == 0
    return True


def plan_from(
    sub: Subscription,
    items: list[SubscriptionItem],
    overrides: dict[int, SubscriptionOverride],
    for_date: dt.date,
) -> list[dict]:
    """Pure plan computation over preloaded ACTIVE items + that date's overrides (by
    item id). Shared by the per-sub API and the batched cutoff (no per-sub queries)."""
    if sub.status != "active":
        return []
    if sub.end_date and for_date > sub.end_date:
        return []
    if for_date < sub.start_date:
        return []
    # Aggregate by product_id so a product covered by >1 subscription item produces ONE
    # line (delivery/invoice key by product_id; duplicate lines would be mis-handled).
    by_product: dict[int, float] = {}
    for it in items:
        ov = overrides.get(it.id)
        if ov is not None:
            # An override FORCES the day regardless of cadence (qty 0 = skip); this is how
            # a customer adds a one-off delivery on an otherwise off day.
            qty = float(ov.override_qty)
        elif _matches(it, sub, for_date):
            qty = float(it.default_qty)
        else:
            continue
        if qty > 0:
            by_product[it.product_id] = by_product.get(it.product_id, 0.0) + qty
    return [{"product_id": pid, "qty": qty} for pid, qty in by_product.items()]


async def compute_plan(db: AsyncSession, sub: Subscription, for_date: dt.date) -> list[dict]:
    if sub.status != "active":
        return []
    res = await db.execute(
        select(SubscriptionItem).where(
            SubscriptionItem.subscription_id == sub.id,
            SubscriptionItem.active.is_(True),
        )
    )
    items = list(res.scalars().all())
    item_ids = [i.id for i in items]
    overrides: dict[int, SubscriptionOverride] = {}
    if item_ids:
        res = await db.execute(
            select(SubscriptionOverride).where(
                SubscriptionOverride.subscription_item_id.in_(item_ids),
                SubscriptionOverride.for_date == for_date,
            )
        )
        overrides = {o.subscription_item_id: o for o in res.scalars().all()}
    return plan_from(sub, items, overrides, for_date)


async def assert_standing_change_allowed(db, user_role: str) -> None:
    """Standing-order changes (items, pause/resume/cancel/re-subscribe) are frozen from the
    cutoff hour until TOMORROW'S RUN HAS FINISHED: while it runs, a change could be read
    half-way. Once tomorrow's order is frozen, a change applies from the next undelivered
    day, so it's allowed again (the freeze used to last until midnight). Admins may
    override."""
    if user_role == "admin":
        return
    now = dt.datetime.now(IST)
    if now.hour < settings.CUTOFF_HOUR:
        return
    from sqlalchemy import select

    from app.models.delivery import ScheduleRun

    tomorrow = now.date() + dt.timedelta(days=1)
    cutoff_at = now.replace(hour=settings.CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    # Tonight's run (started after the cutoff) has finished and frozen tomorrow.
    done = await db.scalar(
        select(ScheduleRun.id).where(
            ScheduleRun.for_delivery_date == tomorrow,
            ScheduleRun.status == "frozen",
            ScheduleRun.started_at >= cutoff_at,
            ScheduleRun.finished_at.is_not(None),
        ).limit(1)
    )
    if done is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Changes are frozen after the {settings.CUTOFF_HOUR}:00 cutoff until tomorrow's "
            "orders are fixed; try again shortly",
        )
