"""Price resolution: shop tier -> account tier -> RETAIL default, with volume breaks."""
import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
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


async def resolve_unit_price(
    db: AsyncSession, product_id: int, tier_id: int | None, qty: float,
    on: dt.date | None = None,
) -> float | None:
    """Applicable unit price for the product+tier+qty, or None when NO price is
    configured (no tier, or no ProductPrice row). None is distinct from a legitimately
    zero price (a ProductPrice row with unit_price == 0) so callers can reject an
    unpriced product instead of silently charging ₹0."""
    if tier_id is None:
        return None
    # `on`: price as of that date (a standing order is priced on its delivery date).
    today = on or dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    res = await db.execute(
        select(ProductPrice)
        .where(
            ProductPrice.product_id == product_id,
            ProductPrice.price_tier_id == tier_id,
            ProductPrice.min_qty <= qty,
            ProductPrice.valid_from <= today,
            or_(ProductPrice.valid_to.is_(None), ProductPrice.valid_to >= today),
        )
        .order_by(ProductPrice.min_qty.desc(), ProductPrice.valid_from.desc())
    )
    price = res.scalars().first()
    return float(price.unit_price) if price else None


class PriceBook:
    """All active prices + tiers preloaded once, resolved in memory — lets the
    nightly cutoff price thousands of lines with zero per-line queries."""

    def __init__(self, today: dt.date, prices: list[ProductPrice], retail_tier_id: int | None):
        self.retail_tier_id = retail_tier_id
        self._by_key: dict[tuple[int, int], list[ProductPrice]] = {}
        for p in prices:
            if p.valid_from <= today and (p.valid_to is None or p.valid_to >= today):
                self._by_key.setdefault((p.price_tier_id, p.product_id), []).append(p)
        # Highest min_qty first so the first match <= qty is the applicable break.
        for rows in self._by_key.values():
            rows.sort(key=lambda r: (float(r.min_qty), str(r.valid_from)), reverse=True)

    def tier_for(self, shop: Shop, account: CustomerAccount | None) -> int | None:
        if shop.price_tier_id:
            return shop.price_tier_id
        if account and account.price_tier_id:
            return account.price_tier_id
        return self.retail_tier_id

    def unit_price(self, product_id: int, tier_id: int | None, qty: float) -> float | None:
        """Unit price, or None when NO price is configured (vs a legit zero price) —
        lets the cutoff skip-and-log an unpriced line instead of charging ₹0."""
        if tier_id is None:
            return None
        for p in self._by_key.get((tier_id, product_id), ()):
            if float(p.min_qty) <= qty:
                return float(p.unit_price)
        return None


async def load_price_book(db: AsyncSession, today: dt.date | None = None) -> PriceBook:
    today = today or dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    # Only rows valid on `today` (not the whole price history) are needed.
    prices = (
        await db.execute(
            select(ProductPrice).where(
                ProductPrice.valid_from <= today,
                or_(ProductPrice.valid_to.is_(None), ProductPrice.valid_to >= today),
            )
        )
    ).scalars().all()
    retail = await db.scalar(select(PriceTier.id).where(PriceTier.code == "RETAIL"))
    return PriceBook(today, list(prices), retail)


async def retail_pack_prices(
    db: AsyncSession, packs: list[tuple[int, float]]
) -> dict[tuple[int, float], float | None]:
    """B2C storefront price of each (product_id, pack_size) pack: the RETAIL unit price a
    cart line of qty=pack_size is charged, x pack_size — exactly the cart/checkout line
    total (round(unit_price * qty, 2)); unit prices are GST-inclusive, nothing is added.

    Same rules as resolve_unit_price (RETAIL tier, valid today, highest min_qty <= qty,
    newest valid_from), but ONE query for all packs. None = no applicable RETAIL row for
    that pack (e.g. min_qty 1 vs a 0.5 pack): "price unavailable", never guessed."""
    if not packs:
        return {}
    today = dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    retail = await db.scalar(select(PriceTier.id).where(PriceTier.code == "RETAIL"))
    if retail is None:
        return dict.fromkeys(packs)
    rows = (
        await db.execute(
            select(ProductPrice).where(
                ProductPrice.price_tier_id == retail,
                ProductPrice.product_id.in_({pid for pid, _ in packs}),
                ProductPrice.valid_from <= today,
                or_(ProductPrice.valid_to.is_(None), ProductPrice.valid_to >= today),
            )
        )
    ).scalars().all()
    book = PriceBook(today, list(rows), retail)
    out: dict[tuple[int, float], float | None] = {}
    for pid, pack in packs:
        qty = round(float(pack), 3)  # carts/orders quantize qty to 3 dp before pricing
        unit = book.unit_price(pid, retail, qty)
        out[(pid, pack)] = None if unit is None else round(unit * qty, 2)
    return out
