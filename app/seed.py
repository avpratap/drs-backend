"""Idempotent dev seed: admin, a subscriber with shops, a delivery agent, and products.

Run:  uv run python -m app.seed
"""
import asyncio
import datetime as dt

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.pricing import PriceTier, ProductPrice
from app.models.product import Product
from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
from app.models.user import User
from app.models.variant import ProductVariant

ADMIN = {"phone": "9999900001", "role": "admin", "full_name": "DRS Admin"}
SUBSCRIBER = {"phone": "9999900002", "role": "subscriber", "full_name": "Sharma Stores"}
DELIVERY = {"phone": "9999900003", "role": "delivery", "full_name": "Ramesh (Delivery)"}

SHOP_NAMES = ["Sharma Store - MG Road", "Sharma Store - Market Yard"]

# Prices are PER UNIT of the product's uom (Rs/litre, Rs/kg, Rs/piece), GST-inclusive,
# Saras-style retail rates. A pack's price is unit_price x pack_size — e.g. 500 ml milk =
# 54 x 0.5 = Rs 27 — exactly what the cart charges for qty = pack_size.
PRODUCTS = [
    {"sku": "MILK-500", "name": "Fresh Cow Milk 500ml", "uom": "litre", "pack_size": 0.5, "gst_rate": 0, "hsn_code": "0401"},
    {"sku": "GHEE-500", "name": "Bilona A2 Ghee 500ml", "uom": "litre", "pack_size": 0.5, "gst_rate": 12, "hsn_code": "0405"},
    {"sku": "CHAAS-200", "name": "Buttermilk (Chaas) 200ml", "uom": "litre", "pack_size": 0.2, "gst_rate": 0, "hsn_code": "0403"},
    {"sku": "PANEER-200", "name": "Fresh Paneer 200g", "uom": "kg", "pack_size": 0.2, "gst_rate": 5, "hsn_code": "0406"},
    {"sku": "CHEESE-200", "name": "Processed Cheese 200g", "uom": "kg", "pack_size": 0.2, "gst_rate": 12, "hsn_code": "0406"},
    # Whole-unit (piece) item: integer packs only — the cart rejects fractional pieces.
    {"sku": "KULFI-1PC", "name": "Malai Kulfi Stick", "uom": "piece", "pack_size": 1, "gst_rate": 18, "hsn_code": "2105"},
]

PRICES = {  # sku: (retail, b2b) per unit of uom
    "MILK-500": (54, 50),  # Rs/L
    "GHEE-500": (640, 600),  # Rs/L
    "CHAAS-200": (40, 36),  # Rs/L
    "PANEER-200": (420, 390),  # Rs/kg
    "CHEESE-200": (550, 510),  # Rs/kg
    "KULFI-1PC": (20, 18),  # Rs/piece
}

# Storefront packs (display variants; checkout stays product-level at qty = pack_size).
VARIANTS = {  # product sku: [(variant sku, label, pack_size)]
    "MILK-500": [("MILK-500-V500", "500ml", 0.5), ("MILK-500-V1L", "1L", 1)],
    "GHEE-500": [("GHEE-500-V500", "500ml", 0.5), ("GHEE-500-V1L", "1L", 1)],
    "PANEER-200": [("PANEER-200-V200", "200g", 0.2), ("PANEER-200-V1K", "1kg", 1)],
    "CHAAS-200": [("CHAAS-200-V200", "200ml", 0.2), ("CHAAS-200-V500", "500ml", 0.5),
                  ("CHAAS-200-V1L", "1L", 1)],
    "CHEESE-200": [("CHEESE-200-V200", "200g", 0.2), ("CHEESE-200-V500", "500g", 0.5)],
    # Piece uom: whole packs only (validate_catalog asserts it).
    "KULFI-1PC": [("KULFI-1PC-V1", "1 pc", 1), ("KULFI-1PC-V4", "Pack of 4", 4),
                  ("KULFI-1PC-V6", "Pack of 6", 6)],
}

PRICE_FROM = dt.date(2026, 1, 1)


def _packs(sku: str) -> list[float]:
    """Every pack size sold for a product (its variants, else its own pack_size)."""
    vs = VARIANTS.get(sku)
    if vs:
        return [size for _vsku, _label, size in vs]
    return [next(p["pack_size"] for p in PRODUCTS if p["sku"] == sku)]


def validate_catalog() -> None:
    """Seed data invariants: every pack has a price, and whole-unit uoms sell whole packs."""
    from app.services.orders import is_whole_unit

    for p in PRODUCTS:
        assert p["sku"] in PRICES, f"{p['sku']} has no price"
        if is_whole_unit(p["uom"]):
            for size in _packs(p["sku"]):
                assert float(size) == int(size), f"{p['sku']}: fractional {p['uom']} pack {size}"


def min_qty_for(sku: str) -> float:
    """Base price rung starts at the smallest pack, so every pack resolves a price."""
    return min(_packs(sku))


async def _upsert_user(db, data) -> User:
    res = await db.execute(select(User).where(User.phone == data["phone"]))
    user = res.scalar_one_or_none()
    if user:
        user.role = data["role"]
        user.full_name = data["full_name"]
        return user
    user = User(phone=data["phone"], role=data["role"], full_name=data["full_name"], phone_verified=True)
    db.add(user)
    await db.flush()
    return user


async def main() -> None:
    from app.core.config import settings

    if settings.ENV == "prod":
        # Creates a phone-verified ADMIN on a well-known number and re-roles existing
        # users holding the seed phones: never against production.
        raise SystemExit("refusing to seed a prod database (ENV=prod)")
    async with SessionLocal() as db:
        await seed(db)


async def seed(db) -> None:
    """Seed into an open session (committed here). Separate from main() for tests."""
    validate_catalog()
    await _upsert_user(db, ADMIN)
    subscriber = await _upsert_user(db, SUBSCRIBER)
    delivery = await _upsert_user(db, DELIVERY)

    res = await db.execute(
        select(CustomerAccount).where(CustomerAccount.owner_user_id == subscriber.id)
    )
    account = res.scalar_one_or_none()
    if account is None:
        account = CustomerAccount(
            owner_user_id=subscriber.id, account_type="b2b", billing_mode="postpaid_credit"
        )
        db.add(account)
        await db.flush()

    shops = []
    for name in SHOP_NAMES:
        res = await db.execute(select(Shop).where(Shop.name == name))
        shop = res.scalar_one_or_none()
        if shop is None:
            shop = Shop(account_id=account.id, name=name, address=name, status="active")
            db.add(shop)
            await db.flush()
        shops.append(shop)

    for shop in shops:
        res = await db.execute(
            select(DeliveryAssignment).where(
                DeliveryAssignment.delivery_id == delivery.id,
                DeliveryAssignment.shop_id == shop.id,
            )
        )
        if res.scalar_one_or_none() is None:
            db.add(DeliveryAssignment(delivery_id=delivery.id, shop_id=shop.id, active=True))

    for p in PRODUCTS:
        res = await db.execute(select(Product).where(Product.sku == p["sku"]))
        prod = res.scalar_one_or_none()
        if prod is None:
            db.add(Product(**p))
        else:  # keep re-seeded DBs in line (e.g. uom / pack_size corrections)
            for k, v in p.items():
                setattr(prod, k, v)
    await db.flush()

    # price tiers
    tiers: dict[str, PriceTier] = {}
    for code, name, ttype in [("RETAIL", "Retail", "retail"), ("B2B_STANDARD", "B2B Standard", "b2b")]:
        res = await db.execute(select(PriceTier).where(PriceTier.code == code))
        t = res.scalar_one_or_none()
        if t is None:
            t = PriceTier(code=code, name=name, tier_type=ttype)
            db.add(t)
            await db.flush()
        tiers[code] = t

    account.price_tier_id = tiers["B2B_STANDARD"].id  # subscriber bills at B2B rates

    res = await db.execute(select(Product))
    by_sku = {p.sku: p for p in res.scalars().all()}
    for sku, (retail, b2b) in PRICES.items():
        prod = by_sku[sku]
        min_qty = min_qty_for(sku)
        for tier_code, price in [("RETAIL", retail), ("B2B_STANDARD", b2b)]:
            tid = tiers[tier_code].id
            # One seed row per (product, tier); update in place so an older seed
            # (per-pack prices at min_qty 1) is corrected rather than shadowing it.
            rows = (
                await db.execute(
                    select(ProductPrice).where(
                        ProductPrice.product_id == prod.id,
                        ProductPrice.price_tier_id == tid,
                        ProductPrice.valid_from == PRICE_FROM,
                    ).order_by(ProductPrice.id)
                )
            ).scalars().all()
            for extra in rows[1:]:
                await db.delete(extra)
            await db.flush()
            if rows:
                rows[0].unit_price = price
                rows[0].min_qty = min_qty
            else:
                db.add(
                    ProductPrice(
                        product_id=prod.id,
                        price_tier_id=tid,
                        unit_price=price,
                        min_qty=min_qty,
                        valid_from=PRICE_FROM,
                    )
                )

        for order, (vsku, label, size) in enumerate(VARIANTS.get(sku, [])):
            # `price` is the legacy display field (checkout never reads it); keep it
            # equal to the resolved pack price so nothing shows a stale number.
            pack_price = round(retail * size, 2)
            res = await db.execute(select(ProductVariant).where(ProductVariant.sku == vsku))
            v = res.scalar_one_or_none()
            if v is None:
                db.add(
                    ProductVariant(
                        product_id=prod.id, sku=vsku, label=label, pack_size=size,
                        price=pack_price, sort_order=order, status="active",
                    )
                )
            else:
                v.product_id, v.label, v.pack_size = prod.id, label, size
                v.price, v.sort_order, v.status = pack_price, order, "active"

    await db.commit()

    print("Seed complete.")
    print("  price tiers -> RETAIL, B2B_STANDARD (subscriber on B2B)")
    print(f"  admin      -> phone {ADMIN['phone']} (role=admin)")
    print(f"  subscriber -> phone {SUBSCRIBER['phone']} with {len(shops)} shops")
    print(f"  delivery   -> phone {DELIVERY['phone']} assigned to {len(shops)} shops")
    print(f"  products   -> {len(PRODUCTS)} ({sum(map(len, VARIANTS.values()))} variants)")


if __name__ == "__main__":
    asyncio.run(main())
