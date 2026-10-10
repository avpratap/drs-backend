"""Price resolution: shop tier -> account tier -> RETAIL default, with volume breaks."""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pricing import PriceTier, ProductPrice
from app.models.shop import CustomerAccount, Shop


async def resolve_tier_id(db: AsyncSession, shop: Shop) -> int | None:
    if shop.price_tier_id:
        return shop.price_tier_id
    account = await db.get(CustomerAccount, shop.account_id)
    if account and account.price_tier_id:
        return account.price_tier_id
    res = await db.execute(select(PriceTier.id).where(PriceTier.code == "RETAIL"))
    return res.scalar_one_or_none()


async def resolve_unit_price(db: AsyncSession, product_id: int, tier_id: int | None, qty: float) -> float:
    if tier_id is None:
        return 0.0
    today = dt.date.today()
    res = await db.execute(
        select(ProductPrice)
        .where(
            ProductPrice.product_id == product_id,
            ProductPrice.price_tier_id == tier_id,
            ProductPrice.min_qty <= qty,
            ProductPrice.valid_from <= today,
        )
        .order_by(ProductPrice.min_qty.desc(), ProductPrice.valid_from.desc())
    )
    price = res.scalars().first()
    return float(price.unit_price) if price else 0.0
