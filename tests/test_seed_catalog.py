"""app/seed.py catalog: every seeded pack gets a storefront price (per-unit RETAIL x
pack_size, == the cart line), and none is reported unpriced.

The seed re-roles users and re-tiers the shared subscriber, so it runs against its own
throwaway database (migrated in a subprocess) — never the suite's shared drs_test."""
import os
import subprocess
import sys

import asyncpg
import pytest
from conftest import auth, login
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_DB = "drs_seed_test"
_URL = f"postgresql+asyncpg://drs:drs@localhost:5432/{_DB}"


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


@pytest.fixture
async def seeded(client):
    await _fresh_db()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": _URL}, check=True, capture_output=True,
    )
    from app.core.db import get_db
    from app.main import app
    from app.seed import seed

    engine = create_async_engine(_URL, poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    for _ in range(2):  # idempotent: a re-seed must not duplicate or shadow prices
        async with Session() as db:
            await seed(db)

    async def _seed_db():
        async with Session() as db:
            yield db

    app.dependency_overrides[get_db] = _seed_db
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


def test_seed_catalog_invariants():
    from app.seed import PRICES, PRODUCTS, _packs, min_qty_for, validate_catalog
    from app.services.orders import is_whole_unit

    validate_catalog()
    for p in PRODUCTS:
        assert min_qty_for(p["sku"]) <= min(_packs(p["sku"]))
        if is_whole_unit(p["uom"]):
            assert all(float(s) == int(s) for s in _packs(p["sku"]))
    # Every demo product offers a choice of packs, so the storefront pack selector is
    # exercised on all of them (CHAAS / CHEESE / KULFI used to show a single size).
    for p in PRODUCTS:
        packs = _packs(p["sku"])
        assert len(packs) >= 2 and len(set(packs)) == len(packs), p["sku"]
        assert p["pack_size"] in packs, p["sku"]  # the base pack stays sellable
    # Per-unit rates (Rs/L, Rs/kg), not per-pack: 500 ml milk = 54 x 0.5.
    assert PRICES["MILK-500"][0] == 54


async def test_every_seeded_pack_is_priced(seeded):
    client = seeded
    ah = auth(await login(client, "9999900001"))
    assert (await client.get("/api/v1/admin/catalog/unpriced-packs", headers=ah)).json() == []

    from app.seed import PRICES, PRODUCTS, VARIANTS

    products = {p["sku"]: p for p in (await client.get("/api/v1/products?limit=200")).json()}
    assert set(products) == {p["sku"] for p in PRODUCTS}
    seen_variants = 0
    for sku, prod in products.items():
        retail = PRICES[sku][0]
        full = (await client.get(f"/api/v1/products/{prod['id']}/full")).json()
        assert prod["from_price"] is not None and full["from_price"] == prod["from_price"]
        assert [v["sku"] for v in full["variants"]] == [v[0] for v in VARIANTS.get(sku, [])]
        for v in full["variants"]:
            seen_variants += 1
            assert v["price_available"] is True
            assert v["selling_price"] == round(retail * v["pack_size"], 2)
            assert v["price"] == v["selling_price"]  # legacy field kept in step
        packs = [v["pack_size"] for v in full["variants"]] or [prod["pack_size"]]
        assert prod["from_price"] == round(retail * min(packs), 2)
    assert seen_variants == sum(map(len, VARIANTS.values()))
    assert products["MILK-500"]["from_price"] == 27.0  # 500 ml at Rs 54/L

    # Displayed == charged: a B2C cart line at qty = pack_size, incl. a sub-1 pack and
    # the whole-unit (piece) product.
    ch = auth(await login(client, "9100009901"))
    for sku, qty in [("MILK-500", 0.5), ("PANEER-200", 0.2), ("KULFI-1PC", 1)]:
        r = await client.post(
            "/api/v1/cart/items", json={"product_id": products[sku]["id"], "qty": qty}, headers=ch
        )
        assert r.status_code == 200, r.text
        line = next(i for i in r.json()["items"] if i["product_id"] == products[sku]["id"])
        assert line["line_total"] == round(PRICES[sku][0] * qty, 2)
