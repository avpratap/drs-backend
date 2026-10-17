from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.order import OrderCreate, OrderItemOut, OrderOut
from app.services import orders as order_service

router = APIRouter(prefix="/orders", tags=["orders"])

# Guest checkout is intentionally NOT implemented: all order endpoints require an
# authenticated user. Anonymous ordering needs an anonymous-identity / guest-token
# redesign (and post-hoc OTP claim) that touches auth broadly — deferred.


async def _gst_rates(db: AsyncSession, product_ids: list[int]) -> dict[int, float]:
    if not product_ids:
        return {}
    rows = (
        await db.execute(
            select(Product.id, Product.gst_rate).where(Product.id.in_(set(product_ids)))
        )
    ).all()
    return {pid: float(rate or 0) for pid, rate in rows}


def _apply_tax(view: OrderOut, rates: dict[int, float]) -> None:
    """Fill indicative tax-inclusive fields (unit_price is pre-tax; GST added on top).

    GST is computed on the discounted taxable value: an order-level coupon is
    apportioned across lines by the subtotal ratio, so tax_total and total_incl_tax
    stay consistent with the discounted `total` (these fields are indicative; the GST
    invoice is authoritative)."""
    subtotal = float(view.subtotal) or 0.0
    # Fraction of each line that remains taxable after an order-level discount.
    disc_factor = (float(view.total) / subtotal) if subtotal > 0 else 1.0
    tax_total = 0.0
    for it in view.items:
        it.gst_rate = rates.get(it.product_id, 0.0)
        taxable = it.line_total * disc_factor
        it.gst_amount = round(taxable * it.gst_rate / 100, 2)
        it.price_incl_tax = round(it.unit_price * (1 + it.gst_rate / 100), 2)
        tax_total += it.gst_amount
    view.tax_total = round(tax_total, 2)
    view.total_incl_tax = round(float(view.total) + view.tax_total, 2)


async def _detail(db: AsyncSession, order: Order) -> OrderOut:
    res = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    out = OrderOut.model_validate(order)
    out.items = [OrderItemOut.model_validate(i) for i in res.scalars().all()]
    _apply_tax(out, await _gst_rates(db, [i.product_id for i in out.items]))
    return out


async def _details(db: AsyncSession, orders: list[Order]) -> list[OrderOut]:
    """Build order views with a single batched item query (no N+1)."""
    if not orders:
        return []
    ids = [o.id for o in orders]
    res = await db.execute(select(OrderItem).where(OrderItem.order_id.in_(ids)))
    items = res.scalars().all()
    by_order: dict[int, list[OrderItemOut]] = {}
    for item in items:
        by_order.setdefault(item.order_id, []).append(OrderItemOut.model_validate(item))
    rates = await _gst_rates(db, [i.product_id for i in items])
    views = []
    for o in orders:
        view = OrderOut.model_validate(o)
        view.items = by_order.get(o.id, [])
        _apply_tax(view, rates)
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
    order = await order_service.create_order(
        db, user, data.items, data.address, data.coupon_code
    )
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


class _ReorderItem:
    """Lightweight stand-in matching OrderItemIn's shape for order_service.create_order."""

    def __init__(self, product_id: int, qty: float):
        self.product_id = product_id
        self.qty = qty


@router.post("/{order_id}/reorder", response_model=OrderOut, operation_id="reorder")
async def reorder(
    order_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Clone a past order's items into a fresh order for the same customer."""
    src = await db.get(Order, order_id)
    if src is None or (
        user.role != "admin" and src.account_id not in await _my_account_ids(db, user)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    src_items = (
        await db.execute(select(OrderItem).where(OrderItem.order_id == src.id))
    ).scalars().all()
    if not src_items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Order has no items to reorder")
    items = [_ReorderItem(i.product_id, float(i.qty)) for i in src_items]
    order = await order_service.create_order(db, user, items, None, None)
    return await _detail(db, order)
