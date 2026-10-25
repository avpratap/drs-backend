"""scripts/seed_storefront_demo.py: generated SVG art via storage, banners, articles,
pages, reviews and a demo customer — idempotent, additive, prod-refusing.

Runs on its own throwaway database (migrated + app.seed) because it adds banners,
articles and reviews that shared-DB tests would see."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from conftest import auth, login
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_DB = "drs_demo_seed_test"
_URL = f"postgresql+asyncpg://drs:drs@localhost:5432/{_DB}"
_ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "seed_storefront_demo", _ROOT / "scripts/seed_storefront_demo.py"
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


@pytest.fixture
async def demo(client, tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    await _fresh_db()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=_ROOT,
        env={**os.environ, "DATABASE_URL": _URL}, check=True, capture_output=True,
    )
    from app.core.db import get_db
    from app.main import app
    from app.seed import seed

    engine = create_async_engine(_URL, poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        await seed(db)
    async with engine.begin() as conn:  # an inactive product and an admin-set image
        await conn.execute(text("UPDATE products SET status='inactive' WHERE sku='CHAAS-200'"))
        await conn.execute(text("UPDATE products SET image_url='/files/admin/ghee.jpg' WHERE sku='GHEE-500'"))

    async def _db():
        async with Session() as db:
            yield db

    app.dependency_overrides[get_db] = _db
    try:
        yield _script(), Session, engine, tmp_path
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


async def _count(Session, model, *where) -> int:
    async with Session() as db:
        return await db.scalar(select(func.count()).select_from(model).where(*where))


async def test_demo_seed_is_complete_and_idempotent(demo, client):
    script, Session, _engine, storage_dir = demo
    from app.models.content import Article, Banner, StaticPage
    from app.models.loyalty import GiftCard, PointsLedger
    from app.models.rating import Rating

    first = await script.seed_demo_session(Session)
    assert first["banners"]["created"] == 3 and first["articles"]["created"] == 6
    assert first["pages"]["created"] == 6
    assert set(first["product_images"]["kept_existing"]) == {"GHEE-500"}  # admin image kept

    # Second run creates nothing new and leaves balances on target.
    second = await script.seed_demo_session(Session)
    for key in ("banners", "articles", "pages"):
        assert second[key] == {"created": 0, "updated": 0}
    assert second["ratings"]["created"] == 0
    assert second["demo_customer"]["created"] is False
    for model, n in ((Banner, 3), (Article, 6), (StaticPage, 6), (GiftCard, 1)):
        assert await _count(Session, model) == n
    assert await _count(Session, PointsLedger) == 1  # no extra adjustment on re-run

    # Every generated image is a stored JPEG photo served from /files.
    files = sorted(storage_dir.rglob("*.jpg"))
    assert len(files) == 6 + 3 + 6 + 2  # products, banners, article covers, hero + about
    for f in files:
        assert f.read_bytes()[:3] == b"\xff\xd8\xff"  # JPEG magic bytes

    # --- What the storefront sees (public, no auth) ---
    products = {p["sku"]: p for p in (await client.get("/api/v1/products?limit=200")).json()}
    assert "CHAAS-200" not in products  # inactive: untouched
    assert products["GHEE-500"]["image_url"] == "/files/admin/ghee.jpg"
    assert products["MILK-500"]["image_url"] == "/files/storefront-demo/products/milk-photo.jpg"
    assert products["PANEER-200"]["image_url"].endswith("/products/paneer-photo.jpg")
    assert products["KULFI-1PC"]["image_url"].endswith("/products/milk-photo.jpg")  # fallback
    r = await client.get(products["MILK-500"]["image_url"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/jpeg")

    banners = (await client.get("/api/v1/content/banners")).json()
    assert [b["position"] for b in banners] == [0, 1, 2]
    assert all(b["image_url"].startswith("/files/storefront-demo/banners/") for b in banners)
    articles = (await client.get("/api/v1/content/articles")).json()
    assert len(articles) == 6
    assert all(a["cover_image_url"] and a["author_name"] and a["published_at"] for a in articles)
    assert all(len(a["body_html"]) > 200 and "<h2" in a["body_html"] for a in articles)
    for slug in ("about-us", "faq", "delivery-areas", "terms", "privacy", "contact"):
        assert (await client.get(f"/api/v1/content/pages/{slug}")).status_code == 200

    for sku, p in products.items():
        ratings = (await client.get(f"/api/v1/products/{p['id']}/ratings")).json()
        assert 3 <= ratings["count"] <= 5, sku
        assert all(3 <= x["stars"] <= 5 and x["comment"] for x in ratings["ratings"])
        assert not any(x["verified_purchase"] for x in ratings["ratings"])
        assert p["rating_count"] == ratings["count"] and p["avg_rating"] > 0
    assert await _count(Session, Rating, Rating.status != "approved") == 0

    # --- The demo customer's account pages ---
    ch = auth(await login(client, script.DEFAULT_PHONE))
    billing = (await client.get("/api/v1/billing/me", headers=ch)).json()
    assert billing["wallet_balance"] == 500.0
    assert (await client.get("/api/v1/loyalty", headers=ch)).json()["points_balance"] == 250
    cards = (await client.get("/api/v1/loyalty/gift-cards", headers=ch)).json()
    assert [(c["status"], c["balance"]) for c in cards] == [("active", 500.0)]
    assert len((await client.get("/api/v1/wishlist", headers=ch)).json()) == 3

    # After the customer spends, a re-run tops the wallet and points back up.
    from app.services import billing as billing_svc
    from app.services import loyalty as loyalty_svc

    acct = billing["account_id"]
    async with Session() as db:
        await billing_svc.wallet_adjust(db, acct, -120, "debit_order")
        await loyalty_svc.redeem_points(db, acct, 50)
        await db.commit()
    third = await script.seed_demo_session(Session)
    assert third["demo_customer"]["wallet_balance"] == 500.0
    assert third["demo_customer"]["points"] == 250


async def test_force_replaces_existing_content(demo):
    script, Session, _engine, _dir = demo
    from app.models.content import StaticPage

    await script.seed_demo_session(Session)
    async with Session() as db:
        page = await db.scalar(select(StaticPage).where(StaticPage.slug == "terms"))
        page.body_html = "<p>edited by an admin</p>"
        await db.commit()
    await script.seed_demo_session(Session)  # default: admin edit survives
    async with Session() as db:
        assert (await db.scalar(select(StaticPage.body_html).where(StaticPage.slug == "terms"))) \
            == "<p>edited by an admin</p>"
    out = await script.seed_demo_session(Session, force=True)
    assert out["pages"]["updated"] == 6 and "GHEE-500" in out["product_images"]["set"]


def test_refuses_prod_and_bad_phone(monkeypatch):
    from app.core.config import settings

    script = _script()
    monkeypatch.setattr(settings, "ENV", "prod")
    with pytest.raises(SystemExit):
        script._guard()
    assert script.product_kind("Buttermilk (Chaas) 200ml") == "buttermilk"  # not "milk"
    assert script.product_kind(None, "Malai Paneer") == "cheese"
    assert script.product_kind("dahi") == "curd"
