"""Cart lifecycle: get-or-create active cart, mutate items, preview coupon, checkout.

Checkout reuses app.services.orders.create_order so pricing / coupon reservation /
redemption all go through one authoritative path, then flips the cart to 'converted'.
"""
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cart import Cart, CartItem
from app.models.coupon import Coupon, CouponRedemption
from app.models.order import Order
from app.models.product import Product
from app.models.shop import Shop
from app.models.user import User
from app.schemas.order import OrderItemIn
from app.services import orders as order_service
from app.services.pricing import resolve_tier_id, resolve_unit_price
from app.services.subscription import today_ist


async def get_or_create_active_cart(db: AsyncSession, account_id: int) -> Cart:
    cart = (
        await db.execute(
            select(Cart).where(Cart.account_id == account_id, Cart.status == "active")
        )
    ).scalar_one_or_none()
    if cart is None:
        # Two concurrent first-touches would both insert and collide on the partial
        # unique index (one active cart per account). Create in a savepoint; on
        # collision re-select the row the other transaction committed instead of 500ing.
        try:
            async with db.begin_nested():
                cart = Cart(account_id=account_id, status="active")
                db.add(cart)
                await db.flush()
        except IntegrityError:
            cart = (
                await db.execute(
                    select(Cart).where(
                        Cart.account_id == account_id, Cart.status == "active"
                    )
                )
            ).scalar_one()
    return cart


async def _require_product(db: AsyncSession, product_id: int) -> None:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    if product.status != "active":  # can't add a retired/soft-deleted product
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Product is not available")


async def _items(db: AsyncSession, cart_id: int) -> list[CartItem]:
    return list(
        (
            await db.execute(
                select(CartItem).where(CartItem.cart_id == cart_id).order_by(CartItem.id)
            )
        ).scalars().all()
    )


async def _locked_item(db: AsyncSession, cart_id: int, item_id: int) -> CartItem:
    item = (
        await db.execute(
            select(CartItem)
            .where(CartItem.id == item_id, CartItem.cart_id == cart_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:  # 404 (not 403) so item ids aren't enumerable across carts
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cart item not found")
    return item


async def add_item(db: AsyncSession, cart: Cart, product_id: int, qty: float) -> None:
    await _require_product(db, product_id)
    existing = (
        await db.execute(
            select(CartItem)
            .where(CartItem.cart_id == cart.id, CartItem.product_id == product_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:  # merge same product into one line
        existing.qty = round(float(existing.qty) + qty, 3)
    else:
        db.add(CartItem(cart_id=cart.id, product_id=product_id, qty=qty))


async def set_item_qty(db: AsyncSession, cart: Cart, item_id: int, qty: float) -> None:
    item = await _locked_item(db, cart.id, item_id)
    item.qty = qty


async def remove_item(db: AsyncSession, cart: Cart, item_id: int) -> None:
    item = await _locked_item(db, cart.id, item_id)
    await db.delete(item)


async def _tier_id(db: AsyncSession, account_id: int) -> int | None:
    """Indicative pricing tier for the cart: the account's home shop drives it
    (shop -> account -> RETAIL). Authoritative pricing is re-resolved at checkout."""
    shop = (
        await db.execute(
            select(Shop).where(Shop.account_id == account_id).order_by(Shop.id).limit(1)
        )
    ).scalar_one_or_none()
    if shop is not None:
        return await resolve_tier_id(db, shop)
    from app.models.pricing import PriceTier

    return await db.scalar(select(PriceTier.id).where(PriceTier.code == "RETAIL"))


async def cart_view(db: AsyncSession, cart: Cart) -> dict:
    items = await _items(db, cart.id)
    tier_id = await _tier_id(db, cart.account_id)
    lines = []
    subtotal = 0.0
    for it in items:
        # Indicative display only; None (no price configured) shows as 0 here but
        # checkout re-resolves authoritatively and rejects an unpriced product.
        resolved = await resolve_unit_price(db, it.product_id, tier_id, float(it.qty))
        unit_price = resolved if resolved is not None else 0.0
        line_total = round(unit_price * float(it.qty), 2)
        subtotal += line_total
        lines.append(
            {
                "id": it.id,
                "product_id": it.product_id,
                "qty": float(it.qty),
                "unit_price": unit_price,
                "line_total": line_total,
            }
        )
    return {"id": cart.id, "status": cart.status, "items": lines, "subtotal": round(subtotal, 2)}


async def coupon_preview(db: AsyncSession, code: str, account_id: int, subtotal: float) -> float:
    """Discount this coupon WOULD give — read-only, never reserves/redeems."""
    coupon = (
        await db.execute(select(Coupon).where(Coupon.code == code))
    ).scalar_one_or_none()
    if coupon is None or not coupon.active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid coupon")
    today = today_ist()
    if coupon.valid_from and today < coupon.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Coupon not yet valid")
    if coupon.valid_to and today > coupon.valid_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Coupon expired")
    if subtotal < float(coupon.min_order_value):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Order below minimum ₹{float(coupon.min_order_value):.2f} for this coupon",
        )
    if coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit:
        raise HTTPException(status.HTTP_409_CONFLICT, "Coupon usage limit reached")
    if coupon.per_user_limit is not None:
        used = await db.scalar(
            select(func.count())
            .select_from(CouponRedemption)
            .where(
                CouponRedemption.coupon_id == coupon.id,
                CouponRedemption.account_id == account_id,
            )
        )
        if (used or 0) >= coupon.per_user_limit:
            raise HTTPException(status.HTTP_409_CONFLICT, "Coupon already used")
    if coupon.discount_type == "percent":
        discount = subtotal * float(coupon.value) / 100
        if coupon.max_discount is not None:
            discount = min(discount, float(coupon.max_discount))
    else:
        discount = float(coupon.value)
    return round(min(discount, subtotal), 2)


async def checkout(db: AsyncSession, user: User, address, coupon_code) -> Order:
    account = await order_service._get_or_create_account(db, user)
    cart = await get_or_create_active_cart(db, account.id)
    # Lock the cart row and re-assert it's still active, so two concurrent checkouts
    # can't both convert one cart into two orders. The lock is held until create_order
    # commits (which also persists the 'converted' flip set below), so a racing
    # checkout blocks here and then sees 'converted' -> 409.
    # populate_existing so the locked re-read refreshes the ALREADY-loaded cart's columns
    # (expire_on_commit=False keeps the stale 'active' in the identity map otherwise, and
    # the guard would read cached data — letting a concurrent checkout convert twice).
    locked = (
        await db.execute(
            select(Cart).where(Cart.id == cart.id).with_for_update().execution_options(
                populate_existing=True
            )
        )
    ).scalar_one()
    if locked.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Cart is already being checked out")
    items = await _items(db, locked.id)
    if not items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Cart is empty")
    payload = [OrderItemIn(product_id=i.product_id, qty=float(i.qty)) for i in items]
    # Flip within the locked transaction; create_order's commit persists the order +
    # coupon reservation/redemption + this status change atomically. If create_order
    # raises (e.g. unpriced/retired product), the session rolls back and the cart
    # stays active.
    locked.status = "converted"
    return await order_service.create_order(db, user, payload, address, coupon_code)
