import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.deps.scope import ALL, allowed_shop_ids
from app.models.product import Product
from app.models.shop import CustomerAccount, Shop
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
from app.services import pricing
from app.services import profile as profile_svc
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
    if shop.status != "active":
        # A deactivated shop must not get new standing orders (the cutoff skips it).
        raise HTTPException(status.HTTP_409_CONFLICT, "Shop is not active")
    return shop


async def _sub_or_404(user: User, db: AsyncSession, sub_id: int) -> Subscription:
    sub = await db.get(Subscription, sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    ids = await allowed_shop_ids(user, db)
    if ids is not ALL and sub.shop_id not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    return sub


_WRITER_ROLES = ("subscriber", "customer", "admin")


async def _mutable_sub_or_404(user: User, db: AsyncSession, sub_id: int) -> Subscription:
    """Like _sub_or_404 but for WRITES: a delivery driver's route scope is read-only —
    they must never edit, pause or cancel a customer's standing order."""
    if user.role not in _WRITER_ROLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    return await _sub_or_404(user, db, sub_id)


async def _account_owner(db: AsyncSession, account_id: int) -> User | None:
    account = await db.get(CustomerAccount, account_id)
    return await db.get(User, account.owner_user_id) if account else None


async def _detail(db: AsyncSession, sub: Subscription) -> SubscriptionDetail:
    res = await db.execute(
        select(SubscriptionItem)
        .where(SubscriptionItem.subscription_id == sub.id)
        .order_by(SubscriptionItem.id)
    )
    out = SubscriptionDetail.model_validate(sub)
    out.items = [ItemOut.model_validate(i) for i in res.scalars().all()]
    return out


async def _retail_home_shop(db: AsyncSession, user: User, data: SubscriptionCreate) -> Shop:
    """Get-or-create the retail customer's account + single home shop (their delivery
    address). One home shop per account keeps the retail model one-address-simple; a B2B
    user wanting many locations uses the subscriber role + explicit shop_ids."""
    acct = await db.scalar(
        select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id)
    )
    if acct is None:
        # Two concurrent first-subscriptions would both insert and collide on
        # UNIQUE(owner_user_id). Create in a savepoint; on collision re-select.
        try:
            async with db.begin_nested():
                acct = CustomerAccount(
                    owner_user_id=user.id, account_type=profile_svc.account_type_for(user),
                    billing_mode="prepaid_wallet",
                )
                db.add(acct)
                await db.flush()
        except IntegrityError:
            # The conflicting txn has committed its row (unique violation), so this
            # re-select always finds it — use scalar_one (not scalar) so a surprising
            # miss surfaces loudly instead of a later AttributeError on None.
            acct = (
                await db.execute(
                    select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id)
                )
            ).scalar_one()
    # Match the D2C orders path: a retail account's single home shop is flagged is_home so
    # a later one-time order reuses the SAME shop instead of provisioning a duplicate.
    shop = await db.scalar(
        select(Shop)
        .where(Shop.account_id == acct.id, Shop.is_home.is_(True))
        .order_by(Shop.id)
        .limit(1)
    )
    if shop is None:
        # uq_shops_one_home_per_account: a concurrent first create loses and re-selects.
        try:
            async with db.begin_nested():
                shop = Shop(
                    account_id=acct.id,
                    name=data.home_name or "Home",
                    address=data.home_address,
                    state=data.home_state,
                    pincode=data.home_pincode,
                    is_home=True,
                )
                db.add(shop)
                await db.flush()
        except IntegrityError:
            shop = (
                await db.execute(
                    select(Shop).where(Shop.account_id == acct.id, Shop.is_home.is_(True))
                )
            ).scalar_one()
    return shop


@router.post("", response_model=SubscriptionOut, operation_id="create_subscription")
async def create_subscription(
    data: SubscriptionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role not in ("subscriber", "admin", "customer"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only subscribers manage subscriptions")
    if data.shop_id is not None:
        shop = await _owned_shop_or_404(user, db, data.shop_id)
    elif user.role == "customer":
        # Retail D2C: auto-provision the caller's home shop.
        shop = await _retail_home_shop(db, user, data)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "shop_id is required")
    res = await db.execute(select(Subscription).where(Subscription.shop_id == shop.id))
    sub = res.scalar_one_or_none()
    created = False
    if sub is None:
        # Race-safe: a concurrent create for the same shop may win uq_subscription_shop;
        # on collision re-select the row it committed instead of a 500.
        try:
            async with db.begin_nested():
                sub = Subscription(
                    account_id=shop.account_id,
                    shop_id=shop.id,
                    status="active",
                    start_date=sub_service.today_ist(),
                )
                db.add(sub)
                await db.flush()
                created = True
        except IntegrityError:
            sub = (
                await db.execute(select(Subscription).where(Subscription.shop_id == shop.id))
            ).scalar_one()
    if created:
        owner = await _account_owner(db, shop.account_id) or user
        await notify.emit_event(
            db, "subscription.created", owner, {"shop_id": shop.id},
            account_id=shop.account_id, dedupe_key=f"subscription.created:{sub.id}",
        )
        await db.commit()
        await db.refresh(sub)
    elif sub.status == "cancelled":
        # Re-subscribing to a shop whose subscription was cancelled reactivates it
        # (rather than silently returning the stale 'cancelled' template) — under the
        # same cutoff freeze as resume.
        await sub_service.assert_standing_change_allowed(db, user.role)
        sub.status = "active"
        sub.start_date = sub_service.today_ist()
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
    if len(items) > 200:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Too many items (max 200)"
        )
    sub = await _mutable_sub_or_404(user, db, sub_id)
    await sub_service.assert_standing_change_allowed(db, user.role)
    # Serialize concurrent edits of this standing order (two inserts of the same new
    # product would otherwise collide on uq_subscription_items_sub_product -> 409/500).
    await db.execute(select(Subscription.id).where(Subscription.id == sub.id).with_for_update())
    pids = [it.product_id for it in items]
    if len(pids) != len(set(pids)):
        # Two lines for one product doubled the quantity forever and broke overrides.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Each product may appear only once"
        )
    if pids:
        products = {
            p.id: p
            for p in (await db.execute(select(Product).where(Product.id.in_(pids)))).scalars()
        }
        missing = [pid for pid in pids if pid not in products]
        if missing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown product(s): {missing}")
        inactive = [pid for pid in pids if products[pid].status != "active"]
        if inactive:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Product(s) not available: {inactive}"
            )
        shop = await db.get(Shop, sub.shop_id)
        tier_id = await pricing.resolve_tier_id(db, shop) if shop is not None else None
        # Priced as the cutoff will price them: on the first delivery date (tomorrow), and
        # only lines that actually deliver by default (a qty-0 line only delivers on
        # override days, whose qty is priced then).
        first_day = sub_service.today_ist() + dt.timedelta(days=1)
        unpriced = [
            it.product_id
            for it in items
            if float(it.default_qty) > 0
            and await pricing.resolve_unit_price(
                db, it.product_id, tier_id, float(it.default_qty), on=first_day
            ) is None
        ]
        if unpriced:
            # The cutoff would silently drop these lines every night.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Product(s) have no price for this account: {unpriced}",
            )
    res = await db.execute(
        select(SubscriptionItem).where(SubscriptionItem.subscription_id == sub.id)
    )
    existing = {i.product_id: i for i in res.scalars().all()}
    seen: set[int] = set()
    # Items whose FUTURE one-day overrides must go: a dropped product's (else they come
    # back when it is re-added, since re-adding reactivates the same row and an override
    # forces its date), and a re-added product's (clears orphans left by drops made
    # before this existed). Past/today's overrides stay as history.
    stale_override_items: list[int] = []
    for it in items:
        seen.add(it.product_id)
        e = existing.get(it.product_id)
        if e is not None:
            if not e.active:
                stale_override_items.append(e.id)
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
            if e.active:
                stale_override_items.append(e.id)
            e.active = False
    if stale_override_items:
        await db.execute(
            delete(SubscriptionOverride).where(
                SubscriptionOverride.subscription_item_id.in_(stale_override_items),
                SubscriptionOverride.for_date > sub_service.today_ist(),
            )
        )
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
    sub = await _mutable_sub_or_404(user, db, sub_id)
    sub_service.assert_editable(data.for_date)

    res = await db.execute(
        select(SubscriptionItem).where(
            SubscriptionItem.subscription_id == sub.id,
            SubscriptionItem.product_id == data.product_id,
            SubscriptionItem.active.is_(True),
        ).with_for_update()  # serialize upserts of this item's override (uq item+date)
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
        pending = await db.scalar(
            select(func.count()).select_from(SubscriptionOverride).where(
                SubscriptionOverride.subscription_item_id == item.id,
                SubscriptionOverride.for_date > sub_service.today_ist(),
            )
        )
        if (pending or 0) >= sub_service.MAX_FUTURE_OVERRIDES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"At most {sub_service.MAX_FUTURE_OVERRIDES} future overrides per product",
            )
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
    sub = await _mutable_sub_or_404(user, db, sub_id)
    if sub.status == new_status:
        return sub
    await sub_service.assert_standing_change_allowed(db, user.role)
    if sub.status not in allowed_from:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot {new_status} a {sub.status} subscription"
        )
    sub.status = new_status
    event = _STATUS_EVENT.get(new_status)
    if event is not None:
        # No dedupe_key: pause/resume can legitimately recur and should notify each time
        # (the HTTP idempotency layer already prevents double-execution of one call).
        # Notify the ACCOUNT OWNER (not the actor — e.g. an admin acting on their behalf).
        owner = await _account_owner(db, sub.account_id) or user
        await notify.emit_event(
            db, event, owner, {"shop_id": sub.shop_id}, account_id=sub.account_id,
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
