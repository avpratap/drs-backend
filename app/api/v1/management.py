"""Admin management CRUD: delivery-staff assignment, price tiers/prices, tier
assignment. All admin-gated."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete
from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.billing import CreditLedger, Wallet
from app.models.coupon import Coupon
from app.models.delivery import DeliveryOrder
from app.models.order import Order
from app.models.pricing import PriceTier, ProductPrice
from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.management import (
    AccountBillingOut,
    CouponIn,
    CouponOut,
    CreditSettleIn,
    DeliveryAssignmentIn,
    DeliveryAssignmentOut,
    PriceTierIn,
    PriceTierOut,
    ProductPriceIn,
    ProductPriceOut,
    ShopPatch,
    TierAssignIn,
    UserAdminOut,
    UserPatch,
    WalletAdjustIn,
)
from app.schemas.shop import ShopOut
from app.services import billing as billing_svc
from app.services import dpdp

router = APIRouter(
    prefix="/admin", tags=["management"], dependencies=[Depends(require_role("admin"))]
)


# ---------- Delivery-staff assignment ----------
@router.post(
    "/delivery-assignments",
    response_model=DeliveryAssignmentOut,
    operation_id="assign_delivery",
)
async def assign_delivery(data: DeliveryAssignmentIn, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, data.delivery_id)
    if user is None or user.role != "delivery":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "delivery_id is not a delivery user")
    if await db.get(Shop, data.shop_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    # Reactivate an existing (possibly deactivated) assignment, else create.
    existing = await db.scalar(
        select(DeliveryAssignment).where(
            DeliveryAssignment.delivery_id == data.delivery_id,
            DeliveryAssignment.shop_id == data.shop_id,
        )
    )
    if existing is not None:
        existing.active = True
        row = existing
    else:
        row = DeliveryAssignment(delivery_id=data.delivery_id, shop_id=data.shop_id, active=True)
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get(
    "/delivery-assignments",
    response_model=list[DeliveryAssignmentOut],
    operation_id="list_delivery_assignments",
)
async def list_delivery_assignments(
    delivery_id: int | None = None,
    shop_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(DeliveryAssignment).order_by(DeliveryAssignment.id.desc())
    if delivery_id is not None:
        q = q.where(DeliveryAssignment.delivery_id == delivery_id)
    if shop_id is not None:
        q = q.where(DeliveryAssignment.shop_id == shop_id)
    res = await db.execute(q.limit(page.limit).offset(page.offset))
    return res.scalars().all()


@router.delete(
    "/delivery-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="unassign_delivery",
)
async def unassign_delivery(assignment_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(DeliveryAssignment, assignment_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    row.active = False  # soft-deactivate (keeps history)
    await db.commit()


# ---------- Price tiers ----------
@router.post("/price-tiers", response_model=PriceTierOut, operation_id="create_price_tier")
async def create_price_tier(data: PriceTierIn, db: AsyncSession = Depends(get_db)):
    if await db.scalar(select(PriceTier).where(PriceTier.code == data.code)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Tier code already exists")
    tier = PriceTier(code=data.code, name=data.name, tier_type=data.tier_type)
    db.add(tier)
    await db.commit()
    await db.refresh(tier)
    return tier


@router.get("/price-tiers", response_model=list[PriceTierOut], operation_id="list_price_tiers")
async def list_price_tiers(db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(PriceTier).order_by(PriceTier.id))).scalars().all()


# ---------- Product prices ----------
@router.post(
    "/product-prices", response_model=ProductPriceOut, operation_id="create_product_price"
)
async def create_product_price(data: ProductPriceIn, db: AsyncSession = Depends(get_db)):
    if await db.get(PriceTier, data.price_tier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    if data.valid_to and data.valid_to < data.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid_to before valid_from")
    price = ProductPrice(
        product_id=data.product_id,
        price_tier_id=data.price_tier_id,
        unit_price=data.unit_price,
        min_qty=data.min_qty,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
    )
    db.add(price)
    await db.commit()
    await db.refresh(price)
    await cache_delete("cache:products:active")
    return price


@router.get(
    "/product-prices", response_model=list[ProductPriceOut], operation_id="list_product_prices"
)
async def list_product_prices(
    product_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(ProductPrice).order_by(ProductPrice.id.desc())
    if product_id is not None:
        q = q.where(ProductPrice.product_id == product_id)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


# ---------- Tier assignment ----------
@router.post("/shops/{shop_id}/tier", operation_id="set_shop_tier")
async def set_shop_tier(shop_id: int, data: TierAssignIn, db: AsyncSession = Depends(get_db)):
    shop = await db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    if await db.get(PriceTier, data.price_tier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    shop.price_tier_id = data.price_tier_id
    await db.commit()
    return {"detail": "tier set", "shop_id": shop_id, "price_tier_id": data.price_tier_id}


@router.post("/accounts/{account_id}/tier", operation_id="set_account_tier")
async def set_account_tier(account_id: int, data: TierAssignIn, db: AsyncSession = Depends(get_db)):
    account = await db.get(CustomerAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if await db.get(PriceTier, data.price_tier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    account.price_tier_id = data.price_tier_id
    await db.commit()
    return {"detail": "tier set", "account_id": account_id, "price_tier_id": data.price_tier_id}


# ---------- User management ----------
@router.get("/users", response_model=list[UserAdminOut], operation_id="list_users")
async def list_users(
    role: str | None = None,
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(User).order_by(User.id)
    if role is not None:
        q = q.where(User.role == role)
    if status_filter is not None:
        q = q.where(User.status == status_filter)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@router.patch("/users/{user_id}", response_model=UserAdminOut, operation_id="update_user")
async def update_user(user_id: int, data: UserPatch, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    patch = data.model_dump(exclude_unset=True)
    if "role" in patch:
        user.role = patch["role"]
        user.token_version += 1  # force re-auth so the new role takes effect
    if "status" in patch:
        user.status = patch["status"]
        if patch["status"] == "suspended":
            user.token_version += 1  # kick existing sessions
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/users/{user_id}/erase", operation_id="admin_erase_user")
async def admin_erase_user(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return await dpdp.erase_user(db, user)


# ---------- Coupons ----------
@router.post("/coupons", response_model=CouponOut, operation_id="create_coupon")
async def create_coupon(data: CouponIn, db: AsyncSession = Depends(get_db)):
    if await db.scalar(select(Coupon).where(Coupon.code == data.code)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Coupon code already exists")
    if data.valid_to and data.valid_from and data.valid_to < data.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid_to before valid_from")
    if data.discount_type == "percent" and data.value > 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "percent value must be <= 100")
    coupon = Coupon(**data.model_dump())
    db.add(coupon)
    await db.commit()
    await db.refresh(coupon)
    return coupon


@router.get("/coupons", response_model=list[CouponOut], operation_id="list_coupons")
async def list_coupons(
    active: bool | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Coupon).order_by(Coupon.id.desc())
    if active is not None:
        q = q.where(Coupon.active == active)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@router.post("/coupons/{coupon_id}/deactivate", response_model=CouponOut, operation_id="deactivate_coupon")
async def deactivate_coupon(coupon_id: int, db: AsyncSession = Depends(get_db)):
    coupon = await db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Coupon not found")
    coupon.active = False
    await db.commit()
    await db.refresh(coupon)
    return coupon


# ---------- Account financial controls ----------
@router.get(
    "/accounts/{account_id}/billing",
    response_model=AccountBillingOut,
    operation_id="admin_account_billing",
)
async def admin_account_billing(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(CustomerAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    wallet = await billing_svc.get_or_create_wallet(db, account_id)
    await db.commit()
    return AccountBillingOut(
        account_id=account_id,
        billing_mode=account.billing_mode,
        wallet_balance=float(wallet.balance),
        credit_outstanding=await billing_svc.credit_outstanding(db, account_id),
    )


@router.post("/accounts/{account_id}/wallet/adjust", operation_id="admin_wallet_adjust")
async def admin_wallet_adjust(
    account_id: int, data: WalletAdjustIn, db: AsyncSession = Depends(get_db)
):
    """Admin wallet correction/goodwill (signed). Negative is allowed (override)."""
    if data.amount == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "amount must be non-zero")
    if await db.get(CustomerAccount, account_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    wallet = await billing_svc.wallet_adjust(
        db, account_id, data.amount, f"admin_{data.reason}"[:20], "admin", None,
        allow_negative=True,
    )
    await db.commit()
    return {"account_id": account_id, "wallet_balance": float(wallet.balance)}


@router.post("/accounts/{account_id}/credit/settle", operation_id="admin_settle_credit")
async def admin_settle_credit(
    account_id: int, data: CreditSettleIn, db: AsyncSession = Depends(get_db)
):
    """Record a payment received against a B2B postpaid account's outstanding credit."""
    if await db.get(CustomerAccount, account_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    new_balance = await billing_svc.credit_entry(
        db, account_id, -data.amount, "payment", "settlement", None
    )
    await db.commit()
    return {"account_id": account_id, "credit_outstanding": new_balance}


# ---------- Shop management ----------
@router.get("/shops", response_model=list[ShopOut], operation_id="admin_list_shops")
async def admin_list_shops(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    res = await db.execute(select(Shop).order_by(Shop.id).limit(page.limit).offset(page.offset))
    return res.scalars().all()


@router.patch("/shops/{shop_id}", response_model=ShopOut, operation_id="admin_update_shop")
async def admin_update_shop(shop_id: int, data: ShopPatch, db: AsyncSession = Depends(get_db)):
    shop = await db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(shop, field, value)
    await db.commit()
    await db.refresh(shop)
    return shop


# ---------- Reports ----------
@router.get("/reports/summary", operation_id="reports_summary")
async def reports_summary(db: AsyncSession = Depends(get_db)):
    """At-a-glance operational + financial counters for the admin dashboard."""
    paid_revenue = await db.scalar(
        select(func.coalesce(func.sum(Order.total), 0)).where(Order.payment_status == "paid")
    )
    wallet_float = await db.scalar(select(func.coalesce(func.sum(Wallet.balance), 0)))
    # Outstanding postpaid credit = latest balance per account, summed.
    latest = (
        select(CreditLedger.account_id, func.max(CreditLedger.id).label("mx"))
        .group_by(CreditLedger.account_id)
        .subquery()
    )
    outstanding = await db.scalar(
        select(func.coalesce(func.sum(CreditLedger.balance_after), 0)).join(
            latest, CreditLedger.id == latest.c.mx
        )
    )
    active_subs = await db.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.status == "active")
    )
    shops = await db.scalar(select(func.count()).select_from(Shop))
    delivered = await db.scalar(
        select(func.count()).select_from(DeliveryOrder).where(DeliveryOrder.status == "delivered")
    )
    return {
        "paid_order_revenue": float(paid_revenue or 0),
        "wallet_float": float(wallet_float or 0),
        "credit_outstanding": float(outstanding or 0),
        "active_subscriptions": active_subs or 0,
        "shops": shops or 0,
        "delivered_orders": delivered or 0,
    }
