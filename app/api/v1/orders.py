from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.models.order import Order, OrderItem
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.order import OrderCreate, OrderItemOut, OrderOut
from app.services import orders as order_service

router = APIRouter(prefix="/orders", tags=["orders"])


async def _detail(db: AsyncSession, order: Order) -> OrderOut:
    res = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    out = OrderOut.model_validate(order)
    out.items = [OrderItemOut.model_validate(i) for i in res.scalars().all()]
    return out


async def _details(db: AsyncSession, orders: list[Order]) -> list[OrderOut]:
    """Build order views with a single batched item query (no N+1)."""
    if not orders:
        return []
    ids = [o.id for o in orders]
    res = await db.execute(select(OrderItem).where(OrderItem.order_id.in_(ids)))
    by_order: dict[int, list[OrderItemOut]] = {}
    for item in res.scalars().all():
        by_order.setdefault(item.order_id, []).append(OrderItemOut.model_validate(item))
    views = []
    for o in orders:
        view = OrderOut.model_validate(o)
        view.items = by_order.get(o.id, [])
        views.append(view)
    return views


async def _my_account_ids(db: AsyncSession, user: User) -> list[int]:
    res = await db.execute(
        select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id)
    )
    return list(res.scalars().all())


@router.post("", response_model=OrderOut, operation_id="create_order")
async def create_order(
    data: OrderCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not data.items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Order must have items")
    order = await order_service.create_order(db, user, data.items, data.address)
    return await _detail(db, order)


@router.get("", response_model=list[OrderOut], operation_id="list_orders")
async def list_orders(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    if user.role == "admin":
        query = select(Order)
    else:
        account_ids = await _my_account_ids(db, user)
        if not account_ids:
            return []
        query = select(Order).where(Order.account_id.in_(account_ids))
    orders = (
        await db.execute(
            query.order_by(Order.id.desc()).limit(page.limit).offset(page.offset)
        )
    ).scalars().all()
    return await _details(db, list(orders))


@router.get("/{order_id}", response_model=OrderOut, operation_id="get_order")
async def get_order(
    order_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    order = await db.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if user.role != "admin" and order.account_id not in await _my_account_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return await _detail(db, order)
