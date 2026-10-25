"""Shopping cart + checkout (customer-scoped, IDOR-safe)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.orders import _detail as _order_detail
from app.core.db import get_db
from app.core.gst_states import validate_state
from app.deps.auth import get_current_user
from app.deps.idempotency import require_idempotency_key
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderOut
from app.schemas.profile import PINCODE_RE
from app.services import cart as cart_svc
from app.services import orders as order_service
from app.services import shipping

router = APIRouter(prefix="/cart", tags=["cart"])


class CartItemIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=100_000, allow_inf_nan=False)


class CartItemQtyIn(BaseModel):
    qty: float = Field(gt=0, le=100_000, allow_inf_nan=False)


class CouponPreviewIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class CheckoutIn(BaseModel):
    address: str | None = Field(default=None, max_length=1024)
    # Delivery state/pincode → the order's GST place of supply (was dropped at checkout,
    # so an interstate buyer was taxed CGST+SGST).
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, pattern=PINCODE_RE)
    coupon_code: str | None = Field(default=None, max_length=32)

    _v_state = field_validator("state")(lambda v: validate_state(v))


class CartLineOut(BaseModel):
    id: int
    product_id: int
    qty: float
    unit_price: float
    line_total: float


class CartOut(BaseModel):
    id: int
    status: str
    items: list[CartLineOut] = []
    subtotal: float


class CouponPreviewOut(BaseModel):
    code: str
    discount: float
    final_total: float


class QuoteIn(BaseModel):
    coupon_code: str | None = Field(default=None, max_length=32)
    pincode: str | None = Field(default=None, max_length=10)
    weight_kg: float = Field(default=0.5, gt=0, le=100, allow_inf_nan=False)
    cod: bool = False


class QuoteLineOut(BaseModel):
    product_id: int
    qty: float
    unit_price: float
    line_total: float
    gst_rate: float
    gst_amount: float


class QuoteOut(BaseModel):
    items: list[QuoteLineOut] = []
    subtotal: float
    discount: float
    tax_total: float
    total: float            # subtotal - discount = amount payable (GST-inclusive)
    total_incl_tax: float   # == total (prices are GST-inclusive; tax is inside)
    serviceable: bool | None = None
    etd_days: int | None = None
    shipping_rate: float | None = None


async def _account_id(db: AsyncSession, user: User) -> int:
    account = await order_service._get_or_create_account(db, user)
    return account.id


async def _view(db: AsyncSession, user: User) -> CartOut:
    cart = await cart_svc.get_or_create_active_cart(db, await _account_id(db, user))
    return CartOut.model_validate(await cart_svc.cart_view(db, cart))


@router.get("", response_model=CartOut, operation_id="get_cart")
async def get_cart(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    view = await _view(db, user)
    await db.commit()  # persists a freshly-created empty cart
    return view


@router.post("/items", response_model=CartOut, operation_id="add_cart_item")
async def add_cart_item(
    data: CartItemIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    cart = await cart_svc.get_or_create_active_cart(db, await _account_id(db, user))
    await cart_svc.add_item(db, cart, data.product_id, data.qty)
    view = CartOut.model_validate(await cart_svc.cart_view(db, cart))
    await db.commit()
    return view


@router.patch("/items/{item_id}", response_model=CartOut, operation_id="set_cart_item_qty")
async def set_cart_item_qty(
    item_id: int,
    data: CartItemQtyIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cart = await cart_svc.get_or_create_active_cart(db, await _account_id(db, user))
    await cart_svc.set_item_qty(db, cart, item_id, data.qty)
    view = CartOut.model_validate(await cart_svc.cart_view(db, cart))
    await db.commit()
    return view


@router.delete("/items/{item_id}", response_model=CartOut, operation_id="remove_cart_item")
async def remove_cart_item(
    item_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    cart = await cart_svc.get_or_create_active_cart(db, await _account_id(db, user))
    await cart_svc.remove_item(db, cart, item_id)
    view = CartOut.model_validate(await cart_svc.cart_view(db, cart))
    await db.commit()
    return view


@router.post("/coupon-preview", response_model=CouponPreviewOut, operation_id="preview_coupon")
async def preview_coupon(
    data: CouponPreviewIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    account_id = await _account_id(db, user)
    cart = await cart_svc.get_or_create_active_cart(db, account_id)
    view = await cart_svc.cart_view(db, cart)
    discount = await cart_svc.coupon_preview(db, data.code, account_id, view["subtotal"])
    await db.commit()
    return CouponPreviewOut(
        code=data.code, discount=discount, final_total=round(view["subtotal"] - discount, 2)
    )


@router.post("/quote", response_model=QuoteOut, operation_id="cart_quote")
async def cart_quote(
    data: QuoteIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """One-call pre-pay quote the web checkout can show before paying: line totals,
    subtotal, coupon discount, indicative GST (on the discounted base), and — if a
    pincode is given — courier serviceability. Read-only; never reserves the coupon."""
    account_id = await _account_id(db, user)
    cart = await cart_svc.get_or_create_active_cart(db, account_id)
    view = await cart_svc.cart_view(db, cart)
    subtotal = float(view["subtotal"])

    discount = 0.0
    if data.coupon_code:
        discount = await cart_svc.coupon_preview(db, data.coupon_code, account_id, subtotal)
    total = round(subtotal - discount, 2)
    disc_factor = (total / subtotal) if subtotal > 0 else 1.0

    # Prices are GST-INCLUSIVE: back the tax OUT of the discounted line (indicative;
    # the invoice is authoritative). gst_amount = charged - charged/(1+rate/100).
    pids = [ln["product_id"] for ln in view["items"]]
    rates: dict[int, float] = {}
    if pids:
        rows = (
            await db.execute(select(Product.id, Product.gst_rate).where(Product.id.in_(pids)))
        ).all()
        rates = {pid: float(rate or 0) for pid, rate in rows}
    lines: list[QuoteLineOut] = []
    tax_total = 0.0
    for ln in view["items"]:
        rate = rates.get(ln["product_id"], 0.0)
        charged = round(ln["line_total"] * disc_factor, 2)
        gst_amount = round(charged - charged / (1 + rate / 100), 2)
        tax_total += gst_amount
        lines.append(QuoteLineOut(
            product_id=ln["product_id"], qty=ln["qty"], unit_price=ln["unit_price"],
            line_total=ln["line_total"], gst_rate=rate, gst_amount=gst_amount,
        ))
    tax_total = round(tax_total, 2)

    serviceable = etd_days = shipping_rate = None
    if data.pincode:
        svc = await shipping.check_serviceability(data.pincode, data.weight_kg, data.cod)
        serviceable, etd_days, shipping_rate = svc["serviceable"], svc["etd_days"], svc["rate"]

    await db.commit()  # persists a freshly-created empty cart, if any
    return QuoteOut(
        items=lines, subtotal=subtotal, discount=discount, tax_total=tax_total,
        total=total, total_incl_tax=total,  # GST already inside the inclusive price
        serviceable=serviceable, etd_days=etd_days, shipping_rate=shipping_rate,
    )


# Idempotency-Key REQUIRED: a retried checkout must replay, not create a second order.
@router.post(
    "/checkout", response_model=OrderOut, operation_id="checkout_cart",
    dependencies=[Depends(require_idempotency_key)],
)
async def checkout_cart(
    data: CheckoutIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    order = await cart_svc.checkout(
        db, user, data.address, data.coupon_code, state=data.state, pincode=data.pincode
    )
    # Enrich with the GST breakdown (inclusive) so the checkout response matches POST /orders.
    return await _order_detail(db, order)
