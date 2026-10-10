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


async def compute_plan(db: AsyncSession, sub: Subscription, for_date: dt.date) -> list[dict]:
    if sub.status != "active":
        return []
    if sub.end_date and for_date > sub.end_date:
        return []
    if for_date < sub.start_date:
        return []

    res = await db.execute(
        select(SubscriptionItem).where(
            SubscriptionItem.subscription_id == sub.id,
            SubscriptionItem.active.is_(True),
        )
    )
    items = res.scalars().all()
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

    lines: list[dict] = []
    for it in items:
        if not _matches(it, sub, for_date):
            continue
        ov = overrides.get(it.id)
        qty = float(ov.override_qty) if ov is not None else float(it.default_qty)
        if qty > 0:
            lines.append({"product_id": it.product_id, "qty": qty})
    return lines
