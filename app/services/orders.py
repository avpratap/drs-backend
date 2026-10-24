"""One-time order checkout: auto-provisions a retail account + home shop, prices lines."""
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shop import CustomerAccount, Shop
from app.models.user import User
from app.services import coupons as coupon_svc
from app.services import profile as profile_svc
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
                    owner_user_id=user.id, account_type=profile_svc.account_type_for(user), billing_mode="prepaid_wallet"
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


MAX_MONEY = 9_999_999_999.99  # Numeric(12,2) ceiling for line/order money columns
MAX_LINE_QTY = 100_000  # same ceiling as OrderItemIn.qty, applied after merging
# uom is free text: "Piece", "pcs", "packs", "pkt" are the same countable unit.
_WHOLE_UNIT_ALIASES = {"piece", "pc", "pack", "pkt", "unit", "no", "nos", "box", "bottle", "jar"}


def is_whole_unit(uom: str | None) -> bool:
    u = (uom or "").strip().lower().rstrip(".")
    return u in _WHOLE_UNIT_ALIASES or (u.endswith("s") and u[:-1] in _WHOLE_UNIT_ALIASES)


def check_whole_units(product, qty: float) -> None:
    if is_whole_unit(product.uom) and float(qty) != int(float(qty)):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Product {product.id} is sold per {product.uom}; quantity must be a whole number",
        )


async def _get_or_create_home_shop(
    db: AsyncSession,
    account: CustomerAccount,
    address: str | None,
    state: str | None = None,
    pincode: str | None = None,
) -> Shop:
    # D2C orders deliver to the account's dedicated home shop, identified by the is_home
    # FLAG (not the magic name) so a B2B account's real store named "Home" is never picked
    # or clobbered by a D2C checkout.
    res = await db.execute(
        select(Shop)
        .where(Shop.account_id == account.id, Shop.is_home.is_(True))
        .order_by(Shop.id)
        .limit(1)
    )
    shop = res.scalar_one_or_none()
    if shop is None:
        # Race-safe: uq_shops_one_home_per_account lets only one concurrent first order
        # create the home shop; the loser re-selects it instead of making a duplicate.
        try:
            async with db.begin_nested():
                shop = Shop(
                    account_id=account.id, name="Home", address=address,
                    state=state, pincode=pincode, status="active", is_home=True,
                )
                db.add(shop)
                await db.flush()
        except IntegrityError:
            shop = (
                await db.execute(
                    select(Shop).where(Shop.account_id == account.id, Shop.is_home.is_(True))
                )
            ).scalar_one()
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
        # Authoritative per-order place-of-supply (falls back to shop/address state if None).
        ship_state=(state or None),
    )
    db.add(order)
    await db.flush()
    order.order_no = f"DRS{order.id:06d}"

    # Merge duplicate product lines in the request so one product becomes one order line
    # (two lines for the same product otherwise mis-price qty-break tiers and clutter the
    # invoice). Preserve first-seen order for stable output.
    merged: dict[int, float] = {}
    for it in items:
        merged[it.product_id] = merged.get(it.product_id, 0.0) + float(it.qty)
    for product_id, qty in merged.items():
        # Quantize to the stored precision BEFORE pricing (qty is Numeric(12,3)) so the
        # stored qty x unit_price always equals the charged line_total, and cap the
        # MERGED qty (the per-line cap was bypassable by repeating a product).
        q = round(qty, 3)
        if q <= 0 or q > MAX_LINE_QTY:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Quantity for product {product_id} must be in (0, {MAX_LINE_QTY:g}]",
            )
        merged[product_id] = q

    subtotal = 0.0
    for product_id, qty in merged.items():
        product = await db.get(Product, product_id)
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Product {product_id} not found")
        if product.status != "active":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Product {product_id} is not available"
            )
        check_whole_units(product, qty)
        unit_price = await resolve_unit_price(db, product_id, tier_id, qty)
        if unit_price is None:  # no price/tier configured — never charge ₹0 silently
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"No price configured for product {product_id}",
            )
        line_total = round(unit_price * qty, 2)
        subtotal += line_total
        if subtotal > MAX_MONEY:
            # Numeric(12,2) tops out just under 1e10: a clean 422, not a DataError mid-order.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Order value is too large")
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product_id,
                qty=qty,
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


async def has_live_shipment(db: AsyncSession, order_id: int) -> bool:
    """A non-cancelled courier shipment (draft included: a create may be in flight)."""
    import datetime as dt

    from sqlalchemy import or_

    from app.models.shipment import Shipment

    n = await db.scalar(
        select(func.count()).select_from(Shipment).where(
            Shipment.order_id == order_id,
            Shipment.status.notin_(("cancelled", "failed", "rto")),
            # A draft whose create died before reaching the courier (no courier id, lease
            # released or expired) is not live: it must not block cancelling forever.
            or_(
                Shipment.status != "draft",
                Shipment.gateway_shipment_id.is_not(None),
                Shipment.create_lease_until > dt.datetime.now(dt.UTC),
            ),
        )
    )
    return bool(n)
