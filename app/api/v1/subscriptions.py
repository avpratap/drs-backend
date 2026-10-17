import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.deps.scope import ALL, allowed_shop_ids
from app.models.shop import Shop
from app.models.subscription import Subscription, SubscriptionItem, SubscriptionOverride
from app.models.user import User
from app.schemas.subscription import (
    ItemIn,
    ItemOut,
    OverrideIn,
    PlanLine,
    PlanOut,
    SubscriptionCreate,
    SubscriptionDetail,
    SubscriptionOut,
)
from app.services import notifications as notify
from app.services import subscription as sub_service

# Subscription status -> lifecycle notification event.
_STATUS_EVENT = {
    "paused": "subscription.paused",
    "active": "subscription.resumed",
    "cancelled": "subscription.cancelled",
}

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


class OverrideSavedOut(BaseModel):
    detail: str
    product_id: int
    for_date: str
    qty: float


async def _owned_shop_or_404(user: User, db: AsyncSession, shop_id: int) -> Shop:
    ids = await allowed_shop_ids(user, db)
    if ids is not ALL and shop_id not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    shop = await db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    return shop


async def _sub_or_404(user: User, db: AsyncSession, sub_id: int) -> Subscription:
    sub = await db.get(Subscription, sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    ids = await allowed_shop_ids(user, db)
    if ids is not ALL and sub.shop_id not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    return sub


async def _detail(db: AsyncSession, sub: Subscription) -> SubscriptionDetail:
    res = await db.execute(
        select(SubscriptionItem)
        .where(SubscriptionItem.subscription_id == sub.id)
        .order_by(SubscriptionItem.id)
    )
    out = SubscriptionDetail.model_validate(sub)
    out.items = [ItemOut.model_validate(i) for i in res.scalars().all()]
    return out


@router.post("", response_model=SubscriptionOut, operation_id="create_subscription")
async def create_subscription(
    data: SubscriptionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role not in ("subscriber", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only subscribers manage subscriptions")
    shop = await _owned_shop_or_404(user, db, data.shop_id)
    res = await db.execute(select(Subscription).where(Subscription.shop_id == shop.id))
    sub = res.scalar_one_or_none()
    if sub is None:
        sub = Subscription(
            account_id=shop.account_id,
            shop_id=shop.id,
            status="active",
            start_date=sub_service.today_ist(),
        )
        db.add(sub)
        await db.flush()
        await notify.emit_event(
            db, "subscription.created", user, {"shop_id": shop.id},
            account_id=shop.account_id, dedupe_key=f"subscription.created:{sub.id}",
        )
        await db.commit()
        await db.refresh(sub)
    return sub


@router.get("", response_model=list[SubscriptionOut], operation_id="list_subscriptions")
async def list_subscriptions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    ids = await allowed_shop_ids(user, db)
    query = select(Subscription)
    if ids is not ALL:
        if not ids:
            return []
        query = query.where(Subscription.shop_id.in_(ids))
    res = await db.execute(query.order_by(Subscription.id).limit(page.limit).offset(page.offset))
    return res.scalars().all()


@router.get("/{sub_id}", response_model=SubscriptionDetail, operation_id="get_subscription")
async def get_subscription(
    sub_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    sub = await _sub_or_404(user, db, sub_id)
    return await _detail(db, sub)


@router.put("/{sub_id}/items", response_model=SubscriptionDetail, operation_id="set_subscription_items")
async def set_items(
    sub_id: int,
    items: list[ItemIn],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the standing-order lines (upsert by product; drop missing ones)."""
    sub = await _sub_or_404(user, db, sub_id)
    res = await db.execute(
        select(SubscriptionItem).where(SubscriptionItem.subscription_id == sub.id)
    )
    existing = {i.product_id: i for i in res.scalars().all()}
    seen: set[int] = set()
    for it in items:
        seen.add(it.product_id)
        e = existing.get(it.product_id)
        if e is not None:
            e.default_qty = it.default_qty
            e.frequency = it.frequency
            e.interval_days = it.interval_days
            e.days_of_week = it.days_of_week
            e.active = True
        else:
            db.add(
                SubscriptionItem(
                    subscription_id=sub.id,
                    product_id=it.product_id,
                    default_qty=it.default_qty,
                    frequency=it.frequency,
                    interval_days=it.interval_days,
                    days_of_week=it.days_of_week,
                    active=True,
                )
            )
    for pid, e in existing.items():
        if pid not in seen:
            e.active = False
    await db.commit()
    return await _detail(db, sub)


@router.post("/{sub_id}/overrides", operation_id="set_override", response_model=OverrideSavedOut)
async def set_override(
    sub_id: int,
    data: OverrideIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Modify a single date (skip / change qty). Server enforces the next-day cutoff."""
    sub = await _sub_or_404(user, db, sub_id)
    sub_service.assert_editable(data.for_date)

    res = await db.execute(
        select(SubscriptionItem).where(
            SubscriptionItem.subscription_id == sub.id,
            SubscriptionItem.product_id == data.product_id,
            SubscriptionItem.active.is_(True),
        )
    )
    item = res.scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not in this subscription")

    res = await db.execute(
        select(SubscriptionOverride).where(
            SubscriptionOverride.subscription_item_id == item.id,
            SubscriptionOverride.for_date == data.for_date,
        )
    )
    ov = res.scalar_one_or_none()
    if ov is not None:
        ov.override_qty = data.override_qty
        ov.note = data.note
    else:
        db.add(
            SubscriptionOverride(
                subscription_item_id=item.id,
                for_date=data.for_date,
                override_qty=data.override_qty,
                note=data.note,
                created_by=user.id,
            )
        )
    await db.commit()
    return {
        "detail": "override saved",
        "product_id": data.product_id,
        "for_date": str(data.for_date),
        "qty": data.override_qty,
    }


async def _set_status(user, db, sub_id, new_status, allowed_from):
    sub = await _sub_or_404(user, db, sub_id)
    if sub.status == new_status:
        return sub
    if sub.status not in allowed_from:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot {new_status} a {sub.status} subscription"
        )
    sub.status = new_status
    event = _STATUS_EVENT.get(new_status)
    if event is not None:
        # No dedupe_key: pause/resume can legitimately recur and should notify each time
        # (the HTTP idempotency layer already prevents double-execution of one call).
        await notify.emit_event(
            db, event, user, {"shop_id": sub.shop_id}, account_id=sub.account_id,
        )
    await db.commit()
    await db.refresh(sub)
    return sub


@router.post("/{sub_id}/pause", response_model=SubscriptionOut, operation_id="pause_subscription")
async def pause_subscription(
    sub_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return await _set_status(user, db, sub_id, "paused", {"active"})


@router.post("/{sub_id}/resume", response_model=SubscriptionOut, operation_id="resume_subscription")
async def resume_subscription(
    sub_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return await _set_status(user, db, sub_id, "active", {"paused"})


@router.post("/{sub_id}/cancel", response_model=SubscriptionOut, operation_id="cancel_subscription")
async def cancel_subscription(
    sub_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    # Terminal: cancelled subscriptions are never materialized by the cutoff again.
    return await _set_status(user, db, sub_id, "cancelled", {"active", "paused"})


@router.get("/{sub_id}/plan", response_model=PlanOut, operation_id="get_subscription_plan")
async def get_plan(
    sub_id: int,
    date: dt.date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Effective quantities for a date (default = tomorrow): template merged with overrides."""
    sub = await _sub_or_404(user, db, sub_id)
    for_date = date or (sub_service.today_ist() + dt.timedelta(days=1))
    lines = await sub_service.compute_plan(db, sub, for_date)
    return PlanOut(for_date=for_date, lines=[PlanLine(**line) for line in lines])
