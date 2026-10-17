import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shipment import Shipment, ShipmentTrackingEvent
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.order import OrderCreate, OrderItemOut, OrderOut
from app.services import invoicing
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
    """Fill the indicative GST breakdown. Prices are GST-INCLUSIVE (MRP): unit_price and
    line_total already include tax, and total_incl_tax == the discounted `total` the
    customer actually pays. gst_amount is the tax PORTION backed out of each line (an
    order-level coupon is apportioned by the subtotal ratio). These fields are indicative;
    the GST invoice is authoritative."""
    subtotal = float(view.subtotal) or 0.0
    disc_factor = (float(view.total) / subtotal) if subtotal > 0 else 1.0
    tax_total = 0.0
    for it in view.items:
        it.gst_rate = rates.get(it.product_id, 0.0)
        charged = it.line_total * disc_factor  # inclusive amount for this line
        taxable = round(charged / (1 + it.gst_rate / 100), 2)
        it.gst_amount = round(charged - taxable, 2)  # tax already inside the price
        it.price_incl_tax = round(it.unit_price, 2)  # price is the inclusive MRP
        tax_total += it.gst_amount
    view.tax_total = round(tax_total, 2)
    view.total_incl_tax = round(float(view.total), 2)  # GST already inside `total`


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


# ---------- Invoice + live tracking ----------
class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_no: str
    invoice_date: dt.date
    total: float


class TrackingEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: str
    location: str | None = None
    note: str | None = None
    event_time: dt.datetime | None = None


class ShipmentTrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shipment_no: str
    status: str
    courier: str | None = None
    awb: str | None = None
    tracking_url: str | None = None
    events: list[TrackingEventOut] = []


class OrderTrackingOut(BaseModel):
    order_id: int
    order_no: str
    status: str
    payment_status: str
    shipments: list[ShipmentTrackOut] = []


async def _owned_order(db: AsyncSession, order_id: int, user: User) -> Order:
    order = await db.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if user.role != "admin" and order.account_id not in await _my_account_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


@router.post("/{order_id}/cancel", response_model=OrderOut, operation_id="cancel_order")
async def cancel_order(
    order_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Customer cancels their own order while it's still cancellable (not yet paid or
    dispatched). A paid order must be refunded by support instead (409)."""
    res = await db.execute(select(Order).where(Order.id == order_id).with_for_update())
    order = res.scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if user.role != "admin" and order.account_id not in await _my_account_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status == "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "Order already cancelled")
    if order.payment_status == "paid":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Order is already paid; contact support for a refund instead of cancelling",
        )
    if order.status not in ("created", "pending"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Order is {order.status} and can no longer be cancelled"
        )
    order.status = "cancelled"
    await db.commit()
    return await _detail(db, order)


@router.post("/{order_id}/invoice", response_model=InvoiceOut, operation_id="generate_order_invoice")
async def generate_order_invoice(
    order_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Issue (or fetch the existing) GST tax invoice for a paid one-time order. Idempotent."""
    order = await _owned_order(db, order_id, user)
    if order.payment_status != "paid":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Invoice is available only after the order is paid"
        )
    inv = await invoicing.generate_for_order(db, order)
    return inv


@router.get("/{order_id}/tracking", response_model=OrderTrackingOut, operation_id="track_order")
async def track_order(
    order_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Live status for a one-time order: order + payment state plus each courier shipment
    with its latest tracking events (newest first)."""
    order = await _owned_order(db, order_id, user)
    shipments = (
        await db.execute(
            select(Shipment).where(Shipment.order_id == order.id).order_by(Shipment.id.desc())
        )
    ).scalars().all()
    out_shipments: list[ShipmentTrackOut] = []
    for sh in shipments:
        events = (
            await db.execute(
                select(ShipmentTrackingEvent)
                .where(ShipmentTrackingEvent.shipment_id == sh.id)
                .order_by(ShipmentTrackingEvent.id.desc())
                .limit(50)
            )
        ).scalars().all()
        view = ShipmentTrackOut.model_validate(sh)
        view.events = [TrackingEventOut.model_validate(e) for e in events]
        out_shipments.append(view)
    return OrderTrackingOut(
        order_id=order.id,
        order_no=order.order_no,
        status=order.status,
        payment_status=order.payment_status,
        shipments=out_shipments,
    )
