"""Admin view of standing orders across every account. Writes (items, pause/resume/
cancel, overrides) reuse the /subscriptions/{id} endpoints, which admins may call for any
subscription and which are exempt from the cutoff freeze for admins."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, paged, pagination
from app.models.shop import CustomerAccount, Shop
from app.models.subscription import Subscription, SubscriptionItem
from app.models.user import User
from app.schemas.subscription import ItemOut, SubscriptionOut

router = APIRouter(
    prefix="/admin", tags=["admin-subscriptions"], dependencies=[Depends(require_role("admin"))]
)


class AdminSubscriptionRow(SubscriptionOut):
    shop_name: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    item_count: int = 0
    created_at: dt.datetime | None = None


class AdminSubscriptionDetail(AdminSubscriptionRow):
    shop_address: str | None = None
    shop_pincode: str | None = None
    shop_status: str | None = None
    price_tier_id: int | None = None
    items: list[ItemOut] = []


async def _rows(db: AsyncSession, subs: list[Subscription]) -> list[AdminSubscriptionRow]:
    """Enrich a page of subscriptions with shop/customer names and line counts (batched)."""
    if not subs:
        return []
    shops = {
        s.id: s
        for s in (await db.execute(select(Shop).where(Shop.id.in_({x.shop_id for x in subs})))).scalars()
    }
    owners = dict(
        (
            await db.execute(
                select(CustomerAccount.id, User)
                .join(User, User.id == CustomerAccount.owner_user_id)
                .where(CustomerAccount.id.in_({x.account_id for x in subs}))
            )
        ).all()
    )
    counts = dict(
        (
            await db.execute(
                select(SubscriptionItem.subscription_id, func.count())
                .where(
                    SubscriptionItem.subscription_id.in_([x.id for x in subs]),
                    SubscriptionItem.active.is_(True),
                )
                .group_by(SubscriptionItem.subscription_id)
            )
        ).all()
    )
    out = []
    for sub in subs:
        row = AdminSubscriptionRow.model_validate(sub)
        shop = shops.get(sub.shop_id)
        owner = owners.get(sub.account_id)
        row.shop_name = shop.name if shop else None
        row.customer_name = owner.full_name if owner else None
        row.customer_phone = owner.phone if owner else None
        row.item_count = int(counts.get(sub.id, 0))
        out.append(row)
    return out


@router.get(
    "/subscriptions",
    response_model=list[AdminSubscriptionRow],
    operation_id="admin_list_subscriptions",
)
async def admin_list_subscriptions(
    status_filter: str | None = Query(default=None, pattern="^(active|paused|cancelled)$"),
    account_id: int | None = None,
    shop_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Subscription).order_by(Subscription.id.desc())
    if status_filter:
        q = q.where(Subscription.status == status_filter)
    if account_id is not None:
        q = q.where(Subscription.account_id == account_id)
    if shop_id is not None:
        q = q.where(Subscription.shop_id == shop_id)
    return await _rows(db, list(await paged(db, q, page)))


@router.get(
    "/subscriptions/{sub_id}",
    response_model=AdminSubscriptionDetail,
    operation_id="admin_get_subscription",
)
async def admin_get_subscription(sub_id: int, db: AsyncSession = Depends(get_db)):
    sub = await db.get(Subscription, sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    (row,) = await _rows(db, [sub])
    out = AdminSubscriptionDetail.model_validate(row.model_dump())
    shop = await db.get(Shop, sub.shop_id)
    if shop is not None:
        out.shop_address = shop.address
        out.shop_pincode = shop.pincode
        out.shop_status = shop.status
        out.price_tier_id = shop.price_tier_id
    out.items = [
        ItemOut.model_validate(i)
        for i in (
            await db.execute(
                select(SubscriptionItem)
                .where(SubscriptionItem.subscription_id == sub.id)
                .order_by(SubscriptionItem.id)
            )
        ).scalars()
    ]
    return out
