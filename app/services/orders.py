"""One-time order checkout: auto-provisions a retail account + home shop, prices lines."""
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shop import CustomerAccount, Shop
from app.models.user import User
from app.services import coupons as coupon_svc
from app.services.pricing import resolve_tier_id, resolve_unit_price


async def _get_or_create_account(db: AsyncSession, user: User) -> CustomerAccount:
    res = await db.execute(select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id))
    account = res.scalar_one_or_none()
    if account is None:
        # Race-safe: concurrent first-touch collides on uq_customer_accounts_owner →
        # re-select the committed row instead of creating a duplicate / 500ing.
        try:
            async with db.begin_nested():
                account = CustomerAccount(
                    owner_user_id=user.id, account_type="retail", billing_mode="prepaid_wallet"
                )
                db.add(account)
                await db.flush()
        except IntegrityError:
            account = (
                await db.execute(
                    select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id)
                )
            ).scalar_one()
    return account


async def _get_or_create_home_shop(
    db: AsyncSession,
    account: CustomerAccount,
    address: str | None,
    state: str | None = None,
    pincode: str | None = None,
) -> Shop:
    # D2C orders deliver to the account's dedicated "Home" shop. Select it by name so a
    # B2B account's real stores are never picked (and never clobbered) by a D2C checkout.
    res = await db.execute(
        select(Shop)
        .where(Shop.account_id == account.id, Shop.name == "Home")
        .order_by(Shop.id)
        .limit(1)
    )
    shop = res.scalar_one_or_none()
    if shop is None:
        shop = Shop(
            account_id=account.id, name="Home", address=address,
            state=state, pincode=pincode, status="active",
        )
        db.add(shop)
        await db.flush()
    else:
        # Fill MISSING fields from this order (so GST place-of-supply becomes accurate),
        # but never overwrite values the customer already saved.
        if address and not shop.address:
            shop.address = address
        if state and not shop.state:
            shop.state = state
        if pincode and not shop.pincode:
            shop.pincode = pincode
    return shop


async def create_order(
    db: AsyncSession,
    user: User,
    items,
    address: str | None,
    coupon_code: str | None = None,
    *,
    state: str | None = None,
    pincode: str | None = None,
) -> Order:
    # NOTE: one-time orders do NOT reserve or decrement stock at order time. DRS is
    # make-to-order (daily production + standing supply), so stock is consumed at
    # fulfillment, not at checkout — overselling at order time is an accepted,
    # deliberate design choice, not an oversight.
    account = await _get_or_create_account(db, user)
    shop = await _get_or_create_home_shop(db, account, address, state, pincode)
    tier_id = await resolve_tier_id(db, shop)

    # A per-row unique placeholder (not a shared "PENDING" constant) so concurrent
    # inserts don't collide on UNIQUE(order_no) before we assign the real DRS number.
    order = Order(
        order_no=f"PENDING-{uuid4().hex[:20]}",
        account_id=account.id,
        shop_id=shop.id,
        order_type="one_time",
        status="created",
        payment_status="pending",
    )
    db.add(order)
    await db.flush()
    order.order_no = f"DRS{order.id:06d}"

    subtotal = 0.0
    for it in items:
        product = await db.get(Product, it.product_id)
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Product {it.product_id} not found")
        if product.status != "active":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Product {it.product_id} is not available"
            )
        unit_price = await resolve_unit_price(db, it.product_id, tier_id, it.qty)
        if unit_price is None:  # no price/tier configured — never charge ₹0 silently
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"No price configured for product {it.product_id}",
            )
        line_total = round(unit_price * it.qty, 2)
        subtotal += line_total
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=it.product_id,
                qty=it.qty,
                unit_price=unit_price,
                line_total=line_total,
            )
        )

    order.subtotal = subtotal
    discount = 0.0
    coupon = None
    if coupon_code:
        discount, coupon = await coupon_svc.validate_and_reserve(
            db, coupon_code, account.id, subtotal
        )
        order.coupon_code = coupon_code
    order.discount = discount
    order.total = round(subtotal - discount, 2)
    if coupon is not None:
        await coupon_svc.record_redemption(db, coupon, account.id, order.id, discount)
    await db.commit()
    await db.refresh(order)
    return order
