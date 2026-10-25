
"""Admin management CRUD: delivery-staff assignment, price tiers/prices, tier
assignment, account finance, shop mgmt, coupons, users, withdrawals. All admin-gated."""
import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete
from app.core.db import get_db
from app.core.phone import is_indian_mobile, normalize_phone
from app.deps.auth import get_current_user, require_role
from app.deps.idempotency import require_idempotency_key
from app.deps.pagination import Page, paged, pagination
from app.models.billing import CreditLedger, Wallet, WithdrawalRequest
from app.models.coupon import Coupon
from app.models.delivery import DeliveryOrder
from app.models.order import Order
from app.models.pricing import PriceTier, ProductPrice
from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.common import EraseResultOut, IdStatusOut
from app.schemas.management import (
    AccountBillingOut,
    CouponIn,
    CouponOut,
    CouponPatch,
    CreditSettleIn,
    DeliveryAssignmentIn,
    DeliveryAssignmentOut,
    PriceTierIn,
    PriceTierOut,
    PriceTierPatch,
    ProductPriceIn,
    ProductPriceOut,
    ProductPricePatch,
    ShopPatch,
    TierAssignIn,
    UserAdminOut,
    UserPatch,
    WalletAdjustIn,
)
from app.schemas.shop import ShopOut
from app.services import admin_tools as audit_svc
from app.services import billing as billing_svc
from app.services import dpdp
from app.services import refunds as refund_acct

router = APIRouter(
    prefix="/admin", tags=["management"], dependencies=[Depends(require_role("admin"))]
)


def _escape_like(term: str) -> str:
    """Match a search term literally (a `%`/`_` in it must not act as a wildcard)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TierSetShopOut(BaseModel):
    detail: str
    shop_id: int
    price_tier_id: int


class TierSetAccountOut(BaseModel):
    detail: str
    account_id: int
    price_tier_id: int


class WalletBalanceOut(BaseModel):
    account_id: int
    wallet_balance: float


class CreditOutstandingOut(BaseModel):
    account_id: int
    credit_outstanding: float


class WithdrawalAdminOut(BaseModel):
    id: int
    account_id: int
    amount: float
    status: str


class ReportsSummaryOut(BaseModel):
    paid_order_revenue: float
    wallet_float: float
    credit_outstanding: float
    active_subscriptions: int
    shops: int
    delivered_orders: int


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


class AdminDeliveryAssignmentRow(DeliveryAssignmentOut):
    driver_name: str | None = None
    driver_phone: str | None = None
    driver_status: str | None = None
    shop_name: str | None = None
    shop_pincode: str | None = None
    shop_status: str | None = None
    created_at: dt.datetime | None = None


@router.get(
    "/delivery-assignments",
    response_model=list[AdminDeliveryAssignmentRow],
    operation_id="list_delivery_assignments",
)
async def list_delivery_assignments(
    delivery_id: int | None = None,
    shop_id: int | None = None,
    active: bool | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    """Driver <-> shop assignments, newest first, with driver and shop names."""
    q = select(DeliveryAssignment).order_by(DeliveryAssignment.id.desc())
    if delivery_id is not None:
        q = q.where(DeliveryAssignment.delivery_id == delivery_id)
    if shop_id is not None:
        q = q.where(DeliveryAssignment.shop_id == shop_id)
    if active is not None:
        q = q.where(DeliveryAssignment.active.is_(active))
    rows = list(await paged(db, q, page))
    if not rows:
        return []
    drivers = {
        u.id: u
        for u in (await db.execute(select(User).where(User.id.in_({r.delivery_id for r in rows})))).scalars()
    }
    shops = {
        x.id: x for x in (await db.execute(select(Shop).where(Shop.id.in_({r.shop_id for r in rows})))).scalars()
    }
    out = []
    for r in rows:
        row = AdminDeliveryAssignmentRow.model_validate(r)
        driver, shop = drivers.get(r.delivery_id), shops.get(r.shop_id)
        if driver is not None:
            row.driver_name, row.driver_phone, row.driver_status = driver.full_name, driver.phone, driver.status
        if shop is not None:
            row.shop_name, row.shop_pincode, row.shop_status = shop.name, shop.pincode, shop.status
        out.append(row)
    return out


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
    return (await db.execute(select(PriceTier).order_by(PriceTier.id).limit(500))).scalars().all()


@router.get("/price-tiers/{tier_id}", response_model=PriceTierOut, operation_id="get_price_tier")
async def get_price_tier(tier_id: int, db: AsyncSession = Depends(get_db)):
    tier = await db.get(PriceTier, tier_id)
    if tier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    return tier


@router.patch("/price-tiers/{tier_id}", response_model=PriceTierOut, operation_id="update_price_tier")
async def update_price_tier(tier_id: int, data: PriceTierPatch, db: AsyncSession = Depends(get_db)):
    tier = await db.get(PriceTier, tier_id)
    if tier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(tier, field, value)
    await db.commit()
    await db.refresh(tier)
    return tier


@router.delete(
    "/price-tiers/{tier_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_price_tier",
)
async def delete_price_tier(tier_id: int, db: AsyncSession = Depends(get_db)):
    tier = await db.get(PriceTier, tier_id)
    if tier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    # Refuse to delete a tier still referenced by prices/shops/accounts (would orphan).
    for model, col in ((ProductPrice, ProductPrice.price_tier_id),
                       (Shop, Shop.price_tier_id),
                       (CustomerAccount, CustomerAccount.price_tier_id)):
        if await db.scalar(select(func.count()).select_from(model).where(col == tier_id)):
            raise HTTPException(status.HTTP_409_CONFLICT, "Tier is in use; reassign first")
    await db.delete(tier)
    await db.commit()


# ---------- Product prices ----------
@router.post(
    "/product-prices", response_model=ProductPriceOut, operation_id="create_product_price"
)
async def create_product_price(data: ProductPriceIn, db: AsyncSession = Depends(get_db)):
    from app.models.product import Product
    from app.services.pricing import default_retail_min_qty

    tier = await db.get(PriceTier, data.price_tier_id)
    if tier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    if data.valid_to and data.valid_to < data.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid_to before valid_from")
    min_qty = data.min_qty
    if min_qty is None:
        if tier.tier_type == "retail":
            # The old flat default of 1 left every sub-1 pack (0.5 L, 200 g) unpriceable.
            product = await db.get(Product, data.product_id)
            if product is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
            min_qty = await default_retail_min_qty(db, product)
        else:
            min_qty = 1  # B2B volume-break rows keep the historical default
    price = ProductPrice(
        product_id=data.product_id,
        price_tier_id=data.price_tier_id,
        unit_price=data.unit_price,
        min_qty=min_qty,
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
    return await paged(db, q, page)


@router.get(
    "/product-prices/{price_id}", response_model=ProductPriceOut, operation_id="get_product_price"
)
async def get_product_price(price_id: int, db: AsyncSession = Depends(get_db)):
    price = await db.get(ProductPrice, price_id)
    if price is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product price not found")
    return price


@router.patch(
    "/product-prices/{price_id}",
    response_model=ProductPriceOut,
    operation_id="update_product_price",
)
async def update_product_price(
    price_id: int, data: ProductPricePatch, db: AsyncSession = Depends(get_db)
):
    price = await db.get(ProductPrice, price_id)
    if price is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product price not found")
    patch = data.model_dump(exclude_unset=True)
    for field, value in patch.items():
        setattr(price, field, value)
    if price.valid_to and price.valid_to < price.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid_to before valid_from")
    await db.commit()
    await db.refresh(price)
    await cache_delete("cache:products:active")
    return price


@router.delete(
    "/product-prices/{price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_product_price",
)
async def delete_product_price(price_id: int, db: AsyncSession = Depends(get_db)):
    price = await db.get(ProductPrice, price_id)
    if price is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product price not found")
    await db.delete(price)
    await db.commit()
    await cache_delete("cache:products:active")


# ---------- Tier assignment ----------
@router.post("/shops/{shop_id}/tier", operation_id="set_shop_tier", response_model=TierSetShopOut)
async def set_shop_tier(shop_id: int, data: TierAssignIn, db: AsyncSession = Depends(get_db)):
    shop = await db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    if await db.get(PriceTier, data.price_tier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    shop.price_tier_id = data.price_tier_id
    await db.commit()
    return {"detail": "tier set", "shop_id": shop_id, "price_tier_id": data.price_tier_id}


@router.post(
    "/accounts/{account_id}/tier", operation_id="set_account_tier", response_model=TierSetAccountOut
)
async def set_account_tier(account_id: int, data: TierAssignIn, db: AsyncSession = Depends(get_db)):
    account = await db.get(CustomerAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if await db.get(PriceTier, data.price_tier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price tier not found")
    account.price_tier_id = data.price_tier_id
    await db.commit()
    return {"detail": "tier set", "account_id": account_id, "price_tier_id": data.price_tier_id}


class AccountTypeIn(BaseModel):
    account_type: Literal["retail", "b2b"] | None = None
    billing_mode: Literal["prepaid_wallet", "postpaid_credit"] | None = None


class AccountTypeOut(BaseModel):
    account_id: int
    account_type: str
    billing_mode: str


@router.patch(
    "/accounts/{account_id}/type", operation_id="set_account_type", response_model=AccountTypeOut
)
async def set_account_type(account_id: int, data: AccountTypeIn, db: AsyncSession = Depends(get_db)):
    """Correct an account's type / billing mode (accounts were typed by whichever endpoint
    first created them, and nothing else could change it)."""
    account = (
        await db.execute(
            select(CustomerAccount).where(CustomerAccount.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if (
        data.billing_mode == "prepaid_wallet"
        and account.billing_mode == "postpaid_credit"
        and await billing_svc.credit_outstanding(db, account_id) > 0
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Settle the outstanding credit before switching to prepaid"
        )
    if data.account_type is not None:
        account.account_type = data.account_type
    if data.billing_mode is not None:
        account.billing_mode = data.billing_mode
    await db.commit()
    return {"account_id": account.id, "account_type": account.account_type,
            "billing_mode": account.billing_mode}


# ---------- User management ----------
_VALID_ROLES = {"admin", "customer", "subscriber", "delivery"}
# Roles an admin may PROVISION directly (phone-verified, no OTP). Retail "customer"
# accounts must self-register via OTP so an admin can't mint a verified retail identity.
_STAFF_CREATABLE_ROLES = {"admin", "subscriber", "delivery"}


class StaffUserIn(BaseModel):
    phone: str = Field(min_length=8, max_length=16, pattern=r"^\d{8,15}$")
    role: str = Field(default="delivery")
    full_name: str | None = Field(default=None, max_length=255)


@router.post("/users", response_model=UserAdminOut, operation_id="admin_create_user", status_code=201)
async def admin_create_user(data: StaffUserIn, db: AsyncSession = Depends(get_db)):
    """Provision a staff account (delivery/admin/subscriber) by phone. Customers still
    self-register via OTP; this is how drivers/staff get onboarded. The account is
    phone-verified so the user can log in with an OTP immediately."""
    if data.role not in _STAFF_CREATABLE_ROLES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"role must be one of: {', '.join(sorted(_STAFF_CREATABLE_ROLES))} "
            "(retail customers self-register via OTP)",
        )
    # Canonicalize exactly like OTP login does, or "919811122233" is stored as typed and
    # the person's OTP login (normalized to 9811122233) silently creates a new customer.
    phone = normalize_phone(data.phone)
    if not is_indian_mobile(phone):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Indian mobile number")
    if await db.scalar(select(User).where(User.phone == phone)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with this phone already exists")
    user = User(
        phone=phone,
        role=data.role,
        full_name=data.full_name,
        status="active",
        phone_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/users", response_model=list[UserAdminOut], operation_id="list_users")
async def list_users(
    role: str | None = Query(default=None, pattern="^(admin|customer|subscriber|delivery)$"),
    status_filter: str | None = Query(default=None, pattern="^(active|suspended|erased)$"),
    q: str | None = Query(default=None, max_length=64, description="Phone or name contains"),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    query = select(User).order_by(User.id)
    if role is not None:
        query = query.where(User.role == role)
    if status_filter is not None:
        query = query.where(User.status == status_filter)
    if q:
        like = f"%{_escape_like(q)}%"
        query = query.where(User.phone.ilike(like, escape="\\") | User.full_name.ilike(like, escape="\\"))
    return await paged(db, query, page)


@router.patch("/users/{user_id}", response_model=UserAdminOut, operation_id="update_user")
async def update_user(
    user_id: int,
    data: UserPatch,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    patch = data.model_dump(exclude_unset=True)
    changed: dict = {}
    if "role" in patch:
        if patch["role"] not in _VALID_ROLES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"role must be one of: {', '.join(sorted(_VALID_ROLES))}",
            )
        changed["role"] = {"from": user.role, "to": patch["role"]}
        user.role = patch["role"]
        user.token_version += 1  # force re-auth so the new role takes effect
    if user.status == "erased" and patch:
        # Erasure is final (DPDP): an erased identity can't be reactivated or re-roled.
        raise HTTPException(status.HTTP_409_CONFLICT, "Erased accounts can't be modified")
    if "status" in patch:
        changed["status"] = {"from": user.status, "to": patch["status"]}
        user.status = patch["status"]
        if patch["status"] == "suspended":
            user.token_version += 1  # kick existing sessions
    if changed:
        await audit_svc.record_audit(
            db, actor.id, "user_update", "user", user_id, detail=changed
        )
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/users/{user_id}/erase", operation_id="admin_erase_user", response_model=EraseResultOut)
async def admin_erase_user(
    user_id: int,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    # Record the audit BEFORE erasure so the actor/target are captured even though
    # erase_user commits internally (the audit row is flushed into that same txn).
    await audit_svc.record_audit(db, actor.id, "user_erase", "user", user_id)
    try:
        return await dpdp.erase_user(db, user)
    except dpdp.ErasureBlocked as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


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
    return await paged(db, q, page)


@router.get("/coupons/{coupon_id}", response_model=CouponOut, operation_id="get_coupon")
async def get_coupon(coupon_id: int, db: AsyncSession = Depends(get_db)):
    coupon = await db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Coupon not found")
    return coupon


@router.patch("/coupons/{coupon_id}", response_model=CouponOut, operation_id="update_coupon")
async def update_coupon(coupon_id: int, data: CouponPatch, db: AsyncSession = Depends(get_db)):
    coupon = await db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Coupon not found")
    patch = data.model_dump(exclude_unset=True)
    if "value" in patch and coupon.discount_type == "percent" and patch["value"] > 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "percent value must be <= 100")
    for field, value in patch.items():
        setattr(coupon, field, value)
    if coupon.valid_to and coupon.valid_from and coupon.valid_to < coupon.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid_to before valid_from")
    if coupon.usage_limit is not None and coupon.usage_limit < coupon.used_count:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"usage_limit can't be below the {coupon.used_count} uses already made",
        )
    await db.commit()
    await db.refresh(coupon)
    return coupon


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


@router.post(
    "/accounts/{account_id}/wallet/adjust",
    operation_id="admin_wallet_adjust",
    response_model=WalletBalanceOut,
    dependencies=[Depends(require_idempotency_key)],
)
async def admin_wallet_adjust(
    account_id: int,
    data: WalletAdjustIn,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin wallet correction/goodwill (signed). Negative is allowed (override)."""
    if data.amount == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "amount must be non-zero")
    if await db.get(CustomerAccount, account_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    wallet = await billing_svc.wallet_adjust(
        # Fixed ledger type (the free-text reason was cut to 20 chars into txn_type); the
        # full reason and note live in the audit record written below.
        db, account_id, data.amount, "adjustment", "admin", None,
        allow_negative=True,
    )
    await audit_svc.record_audit(
        db, actor.id, "wallet_adjust", "customer_account", account_id,
        detail={"amount": float(data.amount), "reason": data.reason, "note": data.note},
    )
    await db.commit()
    return {"account_id": account_id, "wallet_balance": float(wallet.balance)}


@router.post(
    "/accounts/{account_id}/credit/settle",
    operation_id="admin_settle_credit",
    response_model=CreditOutstandingOut,
    dependencies=[Depends(require_idempotency_key)],
)
async def admin_settle_credit(
    account_id: int,
    data: CreditSettleIn,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record a payment received against a B2B postpaid account's outstanding credit."""
    # Lock the account row FIRST so the overpay check and the credit write are atomic:
    # credit_entry serializes all credit writes for an account on this same row, so holding
    # it here prevents a TOCTOU where two concurrent settlements both read the same
    # `outstanding`, both pass the guard, and together overpay into a negative (drawable)
    # credit balance.
    account = (
        await db.execute(
            select(CustomerAccount).where(CustomerAccount.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.billing_mode != "postpaid_credit":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Account is not on postpaid credit; nothing to settle",
        )
    # Read outstanding UNDER the lock so no concurrent settlement can slip in between.
    outstanding = await billing_svc.credit_outstanding(db, account_id)
    if round(float(data.amount), 2) > round(float(outstanding), 2):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Settlement {data.amount} exceeds outstanding credit {outstanding}",
        )
    new_balance = await billing_svc.credit_entry(
        db, account_id, -data.amount, "payment", "settlement", None
    )
    await audit_svc.record_audit(
        db, actor.id, "credit_settle", "customer_account", account_id,
        detail={"amount": float(data.amount)},
    )
    await db.commit()
    return {"account_id": account_id, "credit_outstanding": new_balance}


# ---------- Shop management ----------
class AdminShopRow(ShopOut):
    price_tier_id: int | None = None
    is_home: bool = False
    customer_name: str | None = None
    customer_phone: str | None = None
    subscription_id: int | None = None
    subscription_status: str | None = None


class ShopDriverOut(BaseModel):
    assignment_id: int
    delivery_id: int
    full_name: str | None = None
    phone: str
    active: bool


class AdminShopDetail(AdminShopRow):
    drivers: list[ShopDriverOut] = []


async def _shop_rows(db: AsyncSession, shops: list[Shop]) -> list[AdminShopRow]:
    if not shops:
        return []
    owners = dict(
        (
            await db.execute(
                select(CustomerAccount.id, User)
                .join(User, User.id == CustomerAccount.owner_user_id)
                .where(CustomerAccount.id.in_({x.account_id for x in shops}))
            )
        ).all()
    )
    subs = {
        sub.shop_id: sub
        for sub in (
            await db.execute(select(Subscription).where(Subscription.shop_id.in_([x.id for x in shops])))
        ).scalars()
    }
    rows = []
    for shop in shops:
        row = AdminShopRow.model_validate(shop)
        owner = owners.get(shop.account_id)
        sub = subs.get(shop.id)
        row.customer_name = owner.full_name if owner else None
        row.customer_phone = owner.phone if owner else None
        row.subscription_id = sub.id if sub else None
        row.subscription_status = sub.status if sub else None
        rows.append(row)
    return rows


@router.get("/shops", response_model=list[AdminShopRow], operation_id="admin_list_shops")
async def admin_list_shops(
    status_filter: str | None = Query(default=None, pattern="^(active|inactive)$"),
    account_id: int | None = None,
    price_tier_id: int | None = None,
    q: str | None = Query(default=None, max_length=64, description="Name or pincode contains"),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    query = select(Shop).order_by(Shop.id)
    if status_filter is not None:
        query = query.where(Shop.status == status_filter)
    if account_id is not None:
        query = query.where(Shop.account_id == account_id)
    if price_tier_id is not None:
        query = query.where(Shop.price_tier_id == price_tier_id)
    if q:
        like = f"%{_escape_like(q)}%"
        query = query.where(Shop.name.ilike(like, escape="\\") | Shop.pincode.ilike(like, escape="\\"))
    return await _shop_rows(db, list(await paged(db, query, page)))


@router.get("/shops/{shop_id}", response_model=AdminShopDetail, operation_id="admin_get_shop")
async def admin_get_shop(shop_id: int, db: AsyncSession = Depends(get_db)):
    """One shop with its owner, price tier, standing order and assigned drivers."""
    shop = await db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    (row,) = await _shop_rows(db, [shop])
    out = AdminShopDetail.model_validate(row.model_dump())
    out.drivers = [
        ShopDriverOut(
            assignment_id=a.id, delivery_id=u.id, full_name=u.full_name, phone=u.phone, active=a.active
        )
        for a, u in (
            await db.execute(
                select(DeliveryAssignment, User)
                .join(User, User.id == DeliveryAssignment.delivery_id)
                .where(DeliveryAssignment.shop_id == shop_id)
                .order_by(DeliveryAssignment.active.desc(), DeliveryAssignment.id)
            )
        ).all()
    ]
    return out


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


# ---------- Wallet withdrawals (approval) ----------
@router.get(
    "/withdrawals", operation_id="admin_list_withdrawals", response_model=list[WithdrawalAdminOut]
)
async def admin_list_withdrawals(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(WithdrawalRequest).order_by(WithdrawalRequest.id.desc())
    if status_filter:
        q = q.where(WithdrawalRequest.status == status_filter)
    rows = await paged(db, q, page)
    return [
        {"id": w.id, "account_id": w.account_id, "amount": float(w.amount), "status": w.status}
        for w in rows
    ]


@router.post("/withdrawals/{wd_id}/approve", operation_id="approve_withdrawal", response_model=IdStatusOut)
async def approve_withdrawal(
    wd_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(WithdrawalRequest).where(WithdrawalRequest.id == wd_id).with_for_update()
    )
    wd = res.scalar_one_or_none()
    if wd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Withdrawal not found")
    if wd.status != "requested":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Withdrawal is {wd.status}")
    wallet = await billing_svc.get_or_create_wallet(db, wd.account_id, lock=True)
    if float(wd.amount) > await billing_svc.withdrawable_cash(db, wd.account_id, float(wallet.balance)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Withdrawal exceeds the cash withdrawable from the wallet"
        )
    try:
        await billing_svc.wallet_adjust(
            db, wd.account_id, -float(wd.amount), "withdrawal", "withdrawal", wd.id
        )
    except billing_svc.InsufficientBalance:
        raise HTTPException(status.HTTP_409_CONFLICT, "Insufficient wallet balance") from None
    wd.status = "paid"
    wd.decided_by = user.id
    wd.decided_at = dt.datetime.now(dt.UTC)
    await audit_svc.record_audit(
        db, user.id, "withdrawal_approve", "withdrawal", wd.id,
        detail={"account_id": wd.account_id, "amount": float(wd.amount)},
    )
    await db.commit()
    return {"id": wd.id, "status": wd.status}


@router.post("/withdrawals/{wd_id}/reject", operation_id="reject_withdrawal", response_model=IdStatusOut)
async def reject_withdrawal(
    wd_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Lock the row (like approve) so a concurrent approve+reject can't interleave into a
    # paid-out-but-"rejected" state.
    wd = (
        await db.execute(
            select(WithdrawalRequest).where(WithdrawalRequest.id == wd_id).with_for_update()
        )
    ).scalar_one_or_none()
    if wd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Withdrawal not found")
    if wd.status != "requested":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Withdrawal is {wd.status}")
    wd.status = "rejected"
    wd.decided_by = user.id
    wd.decided_at = dt.datetime.now(dt.UTC)
    await audit_svc.record_audit(
        db, user.id, "withdrawal_reject", "withdrawal", wd.id,
        detail={"account_id": wd.account_id, "amount": float(wd.amount)},
    )
    await db.commit()
    return {"id": wd.id, "status": wd.status}


# ---------- Reports ----------
@router.get("/reports/summary", operation_id="reports_summary", response_model=ReportsSummaryOut)
async def reports_summary(db: AsyncSession = Depends(get_db)):
    """At-a-glance operational + financial counters for the admin dashboard."""
    paid_revenue = await db.scalar(
        select(func.coalesce(func.sum(refund_acct.net_order_value()), 0)).where(
            Order.payment_status.in_(refund_acct.SETTLED_STATUSES)
        )
    )
    wallet_float = await db.scalar(select(func.coalesce(func.sum(Wallet.balance), 0)))
    # Outstanding postpaid credit = latest balance per account, summed.
    latest = (
        select(CreditLedger.account_id, func.max(CreditLedger.id).label("mx"))
        .group_by(CreditLedger.account_id)
        .subquery()
    )
    # Only accounts that OWE money count toward outstanding — an overpaid (negative)
    # account's credit balance must not net down the total others owe.
    outstanding = await db.scalar(
        select(func.coalesce(func.sum(func.greatest(CreditLedger.balance_after, 0)), 0)).join(
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


# ---------- Catalog: packs the storefront can't price ----------
class UnpricedPackOut(BaseModel):
    product_id: int
    product_sku: str
    variant_id: int | None = None  # None = the product's own pack (no variants)
    variant_sku: str | None = None
    label: str | None = None
    pack_size: float
    reason: str


@router.get(
    "/catalog/unpriced-packs",
    response_model=list[UnpricedPackOut],
    operation_id="admin_unpriced_packs",
)
async def admin_unpriced_packs(db: AsyncSession = Depends(get_db)):
    """Every ACTIVE sellable pack the B2C storefront shows as "price unavailable": no
    RETAIL price row applies at its pack size today (none configured, none valid today,
    or min_qty above the pack size — e.g. min_qty 1 vs a 0.5 L pack)."""
    from app.models.product import Product
    from app.models.variant import ProductVariant
    from app.services import pricing

    products = (
        await db.execute(select(Product).where(Product.status == "active").order_by(Product.id))
    ).scalars().all()
    variants = (
        await db.execute(
            select(ProductVariant)
            .where(ProductVariant.status == "active")
            .order_by(ProductVariant.product_id, ProductVariant.sort_order, ProductVariant.id)
        )
    ).scalars().all()
    by_product: dict[int, list] = {}
    for v in variants:
        by_product.setdefault(v.product_id, []).append(v)
    packs: list[tuple] = []  # (product, variant | None, pack_size)
    for p in products:
        vs = by_product.get(p.id)
        packs += [(p, v, float(v.pack_size)) for v in vs] if vs else [(p, None, float(p.pack_size))]
    prices = await pricing.retail_pack_prices(db, [(p.id, size) for p, _v, size in packs])
    return [
        UnpricedPackOut(
            product_id=p.id, product_sku=p.sku,
            variant_id=v.id if v else None, variant_sku=v.sku if v else None,
            label=v.label if v else None, pack_size=size,
            reason="no RETAIL price row applies at this pack size today",
        )
        for p, v, size in packs
        if prices.get((p.id, size)) is None
    ]
