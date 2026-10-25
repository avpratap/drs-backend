"""0058 backfill: legacy retail prices at min_qty 1 are lowered to each product's
smallest active pack, so seeded sub-1 packs get a price. Volume breaks, B2B rows and
base rungs above 1 are untouched; re-running is a no-op.

Runs on its own throwaway database (migrated to 0057, legacy rows planted, then a real
`alembic upgrade head`) so the suite's shared drs_test is never rewritten."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from conftest import auth, login
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_DB = "drs_backfill_test"
_URL = f"postgresql+asyncpg://drs:drs@localhost:5432/{_DB}"
_ROOT = Path(__file__).resolve().parents[1]


def _alembic(rev: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", rev], cwd=_ROOT,
        env={**os.environ, "DATABASE_URL": _URL}, check=True, capture_output=True,
    )


def _script():
    spec = importlib.util.spec_from_file_location(
        "backfill_script", _ROOT / "scripts/backfill_retail_min_qty.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _fresh_db() -> None:
    conn = await asyncpg.connect(user="drs", password="drs", host="localhost", database="drs")
    try:
        await conn.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{_DB}'"
        )
        await conn.execute(f"DROP DATABASE IF EXISTS {_DB}")
        await conn.execute(f"CREATE DATABASE {_DB} OWNER drs")
    finally:
        await conn.close()


async def _rows(engine, sql, **kw):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), kw)).all()


async def _min_qty(engine, sku, tier, *, valid_from=None) -> list[float]:
    rows = await _rows(engine, """
        SELECT pp.min_qty FROM product_prices pp
          JOIN products p ON p.id = pp.product_id JOIN price_tiers t ON t.id = pp.price_tier_id
         WHERE p.sku = :sku AND t.code = :tier ORDER BY pp.min_qty""", sku=sku, tier=tier)
    return [float(r[0]) for r in rows]


@pytest.fixture
async def legacy_engine():
    await _fresh_db()
    _alembic("0057_location_name_unique")
    from app.seed import seed

    engine = create_async_engine(_URL, poolclass=NullPool)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await seed(db)
    async with engine.begin() as conn:
        # Pre-#7/#8 state: every seed price row at the old default min_qty 1.
        await conn.execute(text("UPDATE product_prices SET min_qty = 1"))
        # A retail volume break (must survive) and a product whose base rung is 2 (> 1,
        # deliberate: must survive), both on the RETAIL tier.
        await conn.execute(text("""
            INSERT INTO product_prices (product_id, price_tier_id, unit_price, min_qty, valid_from)
            SELECT p.id, t.id, 50, 10, DATE '2026-01-01' FROM products p, price_tiers t
             WHERE p.sku = 'MILK-500' AND t.code = 'RETAIL'"""))
        await conn.execute(text("""
            UPDATE product_prices SET min_qty = 2 FROM products p, price_tiers t
             WHERE product_prices.product_id = p.id AND product_prices.price_tier_id = t.id
               AND p.sku = 'CHEESE-200' AND t.code = 'RETAIL'"""))
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_backfill_migration_fixes_legacy_retail_rows(legacy_engine, client):
    engine = legacy_engine
    assert await _min_qty(engine, "MILK-500", "RETAIL") == [1.0, 10.0]

    _alembic("head")  # the real 0058 upgrade

    assert await _min_qty(engine, "MILK-500", "RETAIL") == [0.5, 10.0]  # break kept
    assert await _min_qty(engine, "GHEE-500", "RETAIL") == [0.5]
    assert await _min_qty(engine, "CHAAS-200", "RETAIL") == [0.2]
    assert await _min_qty(engine, "PANEER-200", "RETAIL") == [0.2]
    assert await _min_qty(engine, "KULFI-1PC", "RETAIL") == [1.0]  # pack 1: nothing to do
    assert await _min_qty(engine, "CHEESE-200", "RETAIL") == [2.0]  # base > 1: deliberate
    for sku in ("MILK-500", "GHEE-500", "PANEER-200"):
        assert await _min_qty(engine, sku, "B2B_STANDARD") == [1.0]  # B2B untouched

    script = _script()
    assert await script.run(engine) == 0  # idempotent

    # Reusable: a smaller pack added later is picked up by re-running the command.
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO product_variants (product_id, sku, label, pack_size, price, status, sort_order)
            SELECT id, 'GHEE-500-V200', '200ml', 0.2, 128, 'active', 9 FROM products
             WHERE sku = 'GHEE-500'"""))
    assert await script.run(engine) == 1
    assert await _min_qty(engine, "GHEE-500", "RETAIL") == [0.2]

    # Storefront check: put CHEESE back to a normal legacy base so all seeded packs apply,
    # then the unpriced report is empty and every pack resolves a price.
    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE product_prices SET min_qty = 1 FROM products p, price_tiers t
             WHERE product_prices.product_id = p.id AND product_prices.price_tier_id = t.id
               AND p.sku = 'CHEESE-200' AND t.code = 'RETAIL'"""))
    assert await script.run(engine) == 1

    from app.core.db import get_db
    from app.main import app

    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _db():
        async with Session() as db:
            yield db

    app.dependency_overrides[get_db] = _db
    try:
        ah = auth(await login(client, "9999900001"))
        assert (await client.get("/api/v1/admin/catalog/unpriced-packs", headers=ah)).json() == []
        for p in (await client.get("/api/v1/products?limit=200")).json():
            assert p["from_price"] is not None, p["sku"]
            full = (await client.get(f"/api/v1/products/{p['id']}/full")).json()
            assert all(v["price_available"] for v in full["variants"]), p["sku"]
    finally:
        app.dependency_overrides.pop(get_db, None)
