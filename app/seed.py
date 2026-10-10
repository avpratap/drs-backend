"""Idempotent dev seed: admin, a subscriber with shops, a delivery agent, and products.

Run:  python -m uv run python -m app.seed
"""
import asyncio
import datetime as dt

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.pricing import PriceTier, ProductPrice
from app.models.product import Product
from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
from app.models.user import User

ADMIN = {"phone": "9999900001", "role": "admin", "full_name": "DRS Admin"}
SUBSCRIBER = {"phone": "9999900002", "role": "subscriber", "full_name": "Sharma Stores"}
DELIVERY = {"phone": "9999900003", "role": "delivery", "full_name": "Ramesh (Delivery)"}

SHOP_NAMES = ["Sharma Store - MG Road", "Sharma Store - Market Yard"]

PRODUCTS = [
    {"sku": "MILK-500", "name": "Fresh Cow Milk 500ml", "uom": "litre", "pack_size": 0.5, "gst_rate": 0, "hsn_code": "0401"},
    {"sku": "GHEE-500", "name": "Bilona A2 Ghee 500ml", "uom": "litre", "pack_size": 0.5, "gst_rate": 12, "hsn_code": "0405"},
    {"sku": "CHAAS-200", "name": "Buttermilk (Chaas) 200ml", "uom": "litre", "pack_size": 0.2, "gst_rate": 0, "hsn_code": "0403"},
    {"sku": "PANEER-200", "name": "Fresh Paneer 200g", "uom": "kg", "pack_size": 0.2, "gst_rate": 5, "hsn_code": "0406"},
    {"sku": "CHEESE-200", "name": "Processed Cheese 200g", "uom": "kg", "pack_size": 0.2, "gst_rate": 12, "hsn_code": "0406"},
]


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
    async with SessionLocal() as db:
        admin = await _upsert_user(db, ADMIN)
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
            if res.scalar_one_or_none() is None:
                db.add(Product(**p))

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

        prices = {  # sku: (retail, b2b)
            "MILK-500": (30, 27),
            "GHEE-500": (400, 370),
            "CHAAS-200": (15, 13),
            "PANEER-200": (90, 82),
            "CHEESE-200": (120, 110),
        }
        res = await db.execute(select(Product))
        by_sku = {p.sku: p for p in res.scalars().all()}
        for sku, (retail, b2b) in prices.items():
            prod = by_sku.get(sku)
            if prod is None:
                continue
            for tier_code, price in [("RETAIL", retail), ("B2B_STANDARD", b2b)]:
                tid = tiers[tier_code].id
                res = await db.execute(
                    select(ProductPrice).where(
                        ProductPrice.product_id == prod.id,
                        ProductPrice.price_tier_id == tid,
                        ProductPrice.min_qty == 1,
                    )
                )
                if res.scalar_one_or_none() is None:
                    db.add(
                        ProductPrice(
                            product_id=prod.id,
                            price_tier_id=tid,
                            unit_price=price,
                            min_qty=1,
                            valid_from=dt.date(2026, 1, 1),
                        )
                    )

        await db.commit()

        print("Seed complete.")
        print(f"  price tiers -> RETAIL, B2B_STANDARD (subscriber on B2B)")
        print(f"  admin      -> phone {ADMIN['phone']} (role=admin)")
        print(f"  subscriber -> phone {SUBSCRIBER['phone']} with {len(shops)} shops")
        print(f"  delivery   -> phone {DELIVERY['phone']} assigned to {len(shops)} shops")
        print(f"  products   -> {len(PRODUCTS)}")


if __name__ == "__main__":
    asyncio.run(main())
