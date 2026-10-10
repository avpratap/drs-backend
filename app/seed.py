"""Idempotent dev seed: admin, a subscriber with shops, a delivery agent, and products.

Run:  python -m uv run python -m app.seed
"""
import asyncio

from sqlalchemy import select

from app.core.db import SessionLocal
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

        await db.commit()

        print("Seed complete.")
        print(f"  admin      -> phone {ADMIN['phone']} (role=admin)")
        print(f"  subscriber -> phone {SUBSCRIBER['phone']} with {len(shops)} shops")
        print(f"  delivery   -> phone {DELIVERY['phone']} assigned to {len(shops)} shops")
        print(f"  products   -> {len(PRODUCTS)}")


if __name__ == "__main__":
    asyncio.run(main())
