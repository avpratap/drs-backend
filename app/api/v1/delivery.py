import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.scope import ALL, allowed_shop_ids
from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.user import User
from app.schemas.delivery import DeliveryOrderItemOut, DeliveryOrderOut
from app.services.subscription import today_ist

router = APIRouter(prefix="/delivery", tags=["delivery"])


@router.get("/orders", response_model=list[DeliveryOrderOut], operation_id="list_delivery_orders")
async def list_delivery_orders(
    date: dt.date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
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
    orders = (await db.execute(query.order_by(DeliveryOrder.id))).scalars().all()

    out: list[DeliveryOrderOut] = []
    for order in orders:
        res = await db.execute(
            select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
        )
        do = DeliveryOrderOut.model_validate(order)
        do.items = [DeliveryOrderItemOut.model_validate(i) for i in res.scalars().all()]
        out.append(do)
    return out
