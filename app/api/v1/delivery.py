import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.deps.scope import ALL, allowed_shop_ids
from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.user import User
from app.schemas.delivery import DeliverRequest, DeliveryOrderItemOut, DeliveryOrderOut
from app.services import delivery as delivery_service
from app.services.subscription import today_ist

router = APIRouter(prefix="/delivery", tags=["delivery"])


async def _order_detail(db: AsyncSession, order: DeliveryOrder) -> DeliveryOrderOut:
    res = await db.execute(
        select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
    )
    out = DeliveryOrderOut.model_validate(order)
    out.items = [DeliveryOrderItemOut.model_validate(i) for i in res.scalars().all()]
    return out


async def _order_details(db: AsyncSession, orders: list[DeliveryOrder]) -> list[DeliveryOrderOut]:
    """Build delivery-order views with a single batched item query (no N+1)."""
    if not orders:
        return []
    ids = [o.id for o in orders]
    res = await db.execute(
        select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id.in_(ids))
    )
    by_order: dict[int, list[DeliveryOrderItemOut]] = {}
    for item in res.scalars().all():
        by_order.setdefault(item.delivery_order_id, []).append(
            DeliveryOrderItemOut.model_validate(item)
        )
    views = []
    for o in orders:
        view = DeliveryOrderOut.model_validate(o)
        view.items = by_order.get(o.id, [])
        views.append(view)
    return views


@router.get("/orders", response_model=list[DeliveryOrderOut], operation_id="list_delivery_orders")
async def list_delivery_orders(
    date: dt.date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    """Delivery orders for a date, scoped to the caller's shops (delivery=assigned,
    subscriber=owned, admin=all)."""
    for_date = date or (today_ist() + dt.timedelta(days=1))
    ids = await allowed_shop_ids(user, db)

    query = select(DeliveryOrder).where(DeliveryOrder.delivery_date == for_date)
    if ids is not ALL:
        if not ids:
            return []
        query = query.where(DeliveryOrder.shop_id.in_(ids))
    orders = (
        await db.execute(
            query.order_by(DeliveryOrder.id).limit(page.limit).offset(page.offset)
        )
    ).scalars().all()
    return await _order_details(db, list(orders))


@router.post(
    "/orders/{order_id}/deliver", response_model=DeliveryOrderOut, operation_id="deliver_order"
)
async def deliver_order(
    order_id: int,
    data: DeliverRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a stop delivered: per-item quantities (partial OK), POD, returns, cash."""
    if user.role not in ("delivery", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only delivery staff can mark deliveries")

    res = await db.execute(
        select(DeliveryOrder).where(DeliveryOrder.id == order_id).with_for_update()
    )
    order = res.scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    ids = await allowed_shop_ids(user, db)
    if ids is not ALL and order.shop_id not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")

    await delivery_service.mark_delivered(db, order, user, data)
    return await _order_detail(db, order)
