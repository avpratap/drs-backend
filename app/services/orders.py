"""One-time order checkout: auto-provisions a retail account + home shop, prices lines."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.shop import CustomerAccount, Shop
from app.models.user import User
from app.services import coupons as coupon_svc
from app.services.pricing import resolve_tier_id, resolve_unit_price


async def _get_or_create_account(db: AsyncSession, user: User) -> CustomerAccount:
    res = await db.execute(select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id))
    account = res.scalar_one_or_none()
    if account is None:
        account = CustomerAccount(
            owner_user_id=user.id, account_type="retail", billing_mode="prepaid_wallet"
        )
        db.add(account)
        await db.flush()
    return account


async def _get_or_create_home_shop(db: AsyncSession, account: CustomerAccount, address: str | None) -> Shop:
    res = await db.execute(
        select(Shop).where(Shop.account_id == account.id).order_by(Shop.id).limit(1)
    )
    shop = res.scalar_one_or_none()
    if shop is None:
        shop = Shop(account_id=account.id, name="Home", address=address, status="active")
        db.add(shop)
        await db.flush()
    elif address:
        shop.address = address
    return shop


async def create_order(
    db: AsyncSession, user: User, items, address: str | None, coupon_code: str | None = None
) -> Order:
    account = await _get_or_create_account(db, user)
    shop = await _get_or_create_home_shop(db, account, address)
    tier_id = await resolve_tier_id(db, shop)

    order = Order(
        order_no="PENDING",
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
        unit_price = await resolve_unit_price(db, it.product_id, tier_id, it.qty)
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
