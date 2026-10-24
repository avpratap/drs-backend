"""Test harness.

Spins up against the real Postgres+PostGIS and Redis from docker-compose, but on
an isolated `drs_test` database and Redis DB 15 — so tests never touch dev data.
The DB is dropped, recreated and migrated once per session, then seeded with
reference rows. NullPool (DB_POOL_MODE=pgbouncer) avoids asyncpg event-loop binding
issues between the sync setup and the async tests.
"""
import asyncio
import datetime as dt
import os

# Must be set BEFORE anything imports app.core.config / app.core.db.
os.environ["ENV"] = "dev"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://drs:drs@localhost:5432/drs_test"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["DB_POOL_MODE"] = "pgbouncer"  # NullPool for tests
os.environ["RATE_LIMIT_ENABLED"] = "false"  # the suite's rapid calls must not self-throttle
# No wall-clock dependence: with the real 23:00 cutoff, any test editing tomorrow /
# a standing order would fail when the suite happens to run 23:00-24:00 IST. Tests that
# exercise the cutoff freeze patch settings.CUTOFF_HOUR explicitly.
os.environ["CUTOFF_HOUR"] = "24"

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


async def _recreate_database() -> None:
    conn = await asyncpg.connect(
        user="drs", password="drs", host="localhost", port=5432, database="drs"
    )
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname='drs_test' AND pid <> pg_backend_pid()"
        )
        await conn.execute("DROP DATABASE IF EXISTS drs_test")
        await conn.execute("CREATE DATABASE drs_test OWNER drs")
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    asyncio.run(_recreate_database())
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(_seed())
    yield


async def _seed() -> None:
    from app.core.db import SessionLocal
    from app.models.pricing import PriceTier, ProductPrice
    from app.models.product import Product
    from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
    from app.models.subscription import Subscription, SubscriptionItem
    from app.models.user import User

    async with SessionLocal() as db:
        users = {
            "admin": User(phone="9999900001", role="admin", status="active", phone_verified=True),
            "subscriber": User(
                phone="9999900002", role="subscriber", status="active", phone_verified=True
            ),
            "delivery": User(
                phone="9999900003", role="delivery", status="active", phone_verified=True
            ),
            "customer": User(
                phone="9876543210", role="customer", status="active", phone_verified=True
            ),
            "other": User(
                phone="9191919191", role="customer", status="active", phone_verified=True
            ),
        }
        for u in users.values():
            db.add(u)
        await db.flush()

        product = Product(sku="MILK-1L", name="Full Cream Milk 1L", uom="litre", gst_rate=0)
        db.add(product)
        # A 12% GST item (ghee) so invoice tax computation is exercisable.
        ghee = Product(
            sku="GHEE-1KG", name="Bilona Ghee 1kg", uom="kg", gst_rate=12, hsn_code="04059020"
        )
        db.add(ghee)
        tier = PriceTier(code="RETAIL", name="Retail", tier_type="retail")
        db.add(tier)
        await db.flush()
        db.add(
            ProductPrice(
                product_id=product.id,
                price_tier_id=tier.id,
                unit_price=27,
                min_qty=1,
                valid_from=dt.date(2025, 1, 1),
            )
        )
        db.add(
            ProductPrice(
                product_id=ghee.id,
                price_tier_id=tier.id,
                unit_price=500,
                min_qty=1,
                valid_from=dt.date(2025, 1, 1),
            )
        )

        account = CustomerAccount(
            owner_user_id=users["subscriber"].id,
            account_type="b2b",
            billing_mode="postpaid_credit",
            price_tier_id=tier.id,
        )
        db.add(account)
        await db.flush()
        shop = Shop(account_id=account.id, name="Corner Store", status="active")
        db.add(shop)
        await db.flush()
        db.add(DeliveryAssignment(delivery_id=users["delivery"].id, shop_id=shop.id, active=True))
        sub = Subscription(
            account_id=account.id,
            shop_id=shop.id,
            status="active",
            start_date=dt.date(2025, 1, 1),
        )
        db.add(sub)
        await db.flush()
        db.add(
            SubscriptionItem(
                subscription_id=sub.id,
                product_id=product.id,
                default_qty=5,
                frequency="daily",
                active=True,
            )
        )
        await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def _redis_lifecycle():
    # redis_client is a module singleton; its connections are bound to the loop
    # that created them. pytest-asyncio uses a fresh loop per test, so we close
    # the pool on teardown and let the next test reconnect on its own loop.
    from app.core.redis import redis_client

    await redis_client.flushdb()
    yield
    await redis_client.aclose()
    # The pool's asyncio.Lock binds to the loop that first CONTENDED it (any concurrent
    # test); the next test's fresh loop then fails with "bound to a different event loop".
    # Reset it so each test's loop gets an unbound lock (a test-harness artifact — prod
    # runs one loop).
    redis_client.connection_pool._lock = asyncio.Lock()


@pytest_asyncio.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client: AsyncClient, phone: str) -> str:
    r = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    otp = r.json()["dev_otp"]
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": phone, "otp": otp})
    return r.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def idem(headers: dict) -> dict:
    """Auth headers + a fresh Idempotency-Key, for endpoints that require one
    (GRN receive, manual wallet/credit money writes)."""
    from uuid import uuid4

    return {**headers, "Idempotency-Key": uuid4().hex}
