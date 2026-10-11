"""Admin management CRUD: delivery-staff assignment, price tiers/prices, tier
assignment. All admin-gated."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete
from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.pricing import PriceTier, ProductPrice
from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
from app.models.user import User
from app.schemas.management import (
    DeliveryAssignmentIn,
    DeliveryAssignmentOut,
    PriceTierIn,
    PriceTierOut,
    ProductPriceIn,
    ProductPriceOut,
    TierAssignIn,
)

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
