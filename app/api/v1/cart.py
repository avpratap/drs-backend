"""Shopping cart + checkout (customer-scoped, IDOR-safe)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.models.order import OrderItem
from app.models.user import User
from app.schemas.order import OrderItemOut, OrderOut
from app.services import cart as cart_svc
from app.services import orders as order_service

router = APIRouter(prefix="/cart", tags=["cart"])


class CartItemIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=100_000, allow_inf_nan=False)


class CartItemQtyIn(BaseModel):
    qty: float = Field(gt=0, le=100_000, allow_inf_nan=False)


class CouponPreviewIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class CheckoutIn(BaseModel):
    address: str | None = None
    coupon_code: str | None = Field(default=None, max_length=32)


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


@router.post("/checkout", response_model=OrderOut, operation_id="checkout_cart")
async def checkout_cart(
    data: CheckoutIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    order = await cart_svc.checkout(db, user, data.address, data.coupon_code)
    res = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    out = OrderOut.model_validate(order)
    out.items = [OrderItemOut.model_validate(i) for i in res.scalars().all()]
    return out
