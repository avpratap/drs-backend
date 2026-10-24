"""Regression tests for the second review round: infra / schema / validation (R2-I*).
IDs refer to scratchpad/MASTER_FIXLIST.md."""
import uuid

import pytest
import sqlalchemy as sa
from conftest import auth, login

from app.core.db import engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


# ---------- R2-I1: the churn run lock really excludes a second run ----------
async def test_churn_lock_blocks_concurrent_run():
    from app.core.db import SessionLocal
    from app.core.redis import redis_client
    from app.services.ml import churn

    await redis_client.set(churn._LOCK_KEY, "someone-else", ex=60)
    try:
        async with SessionLocal() as db:
            counts = await churn.score_all(db) if hasattr(churn, "score_all") else await churn.run(db)
        assert counts["scored"] == 0
        assert await redis_client.get(churn._LOCK_KEY) == "someone-else"  # not released by us
    finally:
        await redis_client.delete(churn._LOCK_KEY)


# ---------- R2-I2: an oversized chunked body is a 413 even when the route reads it ----------
async def test_chunked_body_413_without_idempotency_key(client):
    async def gen():
        chunk = b"x" * (1024 * 1024)
        for _ in range(9):
            yield chunk

    r = await client.post("/api/v1/payments/create", content=gen(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text


# ---------- R2-I3: scheme from the trusted proxy; no ENV disclosure; metrics token ----------
async def test_proxy_scheme_and_root_and_metrics(client, monkeypatch):
    from app.core.config import settings

    root = (await client.get("/")).json()
    assert "env" not in root
    monkeypatch.setattr(settings, "METRICS_TOKEN", "m-secret")
    assert (await client.get("/metrics")).status_code == 404
    assert (await client.get("/metrics", headers={"Authorization": "Bearer m-secret"})).status_code == 200
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    r = await client.get("/api/v1/products/", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
    if r.status_code in (307, 308):
        assert r.headers["location"].startswith("https://")


# ---------- R2-I4: explicit nulls / out-of-range values are clean 422s ----------
async def test_patch_nulls_and_bounds_are_422(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001301"))
    assert (await client.patch("/api/v1/profile", json={"locale": None}, headers=h)).status_code == 422
    pid = (await client.post("/api/v1/products", json={"sku": f"N-{uuid.uuid4().hex[:8]}", "name": "n"},
                             headers=ah)).json()["id"]
    assert (await client.patch(f"/api/v1/products/{pid}", json={"name": None}, headers=ah)).status_code == 422
    assert (await client.patch(f"/api/v1/products/{pid}", json={"gst_rate": 99.995}, headers=ah)).status_code == 422
    assert (await client.patch(f"/api/v1/products/{pid}", json={"pack_size": "Infinity"},
                               headers=ah)).status_code == 422
    tier = (await _q("SELECT id FROM price_tiers WHERE code='RETAIL'"))[0][0]
    pp = await client.post("/api/v1/admin/product-prices", json={
        "product_id": pid, "price_tier_id": tier, "unit_price": 5, "valid_from": "2025-01-01",
        "valid_to": "2030-01-01"}, headers=ah)
    assert pp.status_code == 200, pp.text
    r = await client.patch(f"/api/v1/admin/product-prices/{pp.json()['id']}", json={"valid_from": None},
                           headers=ah)
    assert r.status_code == 422  # was a TypeError 500


async def test_unknown_state_and_bad_pincode_rejected(client):
    h = auth(await login(client, "9000001302"))
    milk = (await _q("SELECT id FROM products WHERE sku='MILK-1L'"))[0][0]
    bad = await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}], "state": "Mars"},
                            headers=h)
    assert bad.status_code == 422
    pin = await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}], "pincode": "12"},
                            headers=h)
    assert pin.status_code == 422
    ok = await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}], "state": "mh"},
                           headers=h)
    assert ok.status_code == 200


async def test_order_value_overflow_is_422(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001303"))
    pid = (await client.post("/api/v1/products", json={"sku": f"BIG-{uuid.uuid4().hex[:8]}", "name": "big"},
                             headers=ah)).json()["id"]
    tier = (await _q("SELECT id FROM price_tiers WHERE code='RETAIL'"))[0][0]
    await _q("INSERT INTO product_prices (product_id, price_tier_id, unit_price, min_qty, valid_from) "
             "VALUES (:p, :t, 999999, 1, '2025-01-01')", p=pid, t=tier)
    r = await client.post("/api/v1/orders", json={"items": [{"product_id": pid, "qty": 99999}]}, headers=h)
    assert r.status_code == 422


# ---------- R2-I5: CSV exports are newest-first and say when they're truncated ----------
async def test_csv_export_truncation_header(client):
    ah = auth(await login(client, "9999900001"))
    r = await client.get("/api/v1/admin/exports/orders.csv", headers=ah)
    assert r.status_code == 200 and r.headers["x-export-truncated"] == "false"
    ids = [int(line.split(",")[0]) for line in r.text.strip().splitlines()[1:]]
    assert ids == sorted(ids, reverse=True)


# ---------- R2-I6: courier webhook tolerates non-string fields ----------
async def test_shipping_webhook_non_string_fields(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001304"))
    milk = (await _q("SELECT id FROM products WHERE sku='MILK-1L'"))[0][0]
    o = (await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}]}, headers=h)).json()
    s = (await client.post("/api/v1/admin/shipments", json={"order_id": o["id"], "pincode": "302001", "cod": True},
                           headers=ah)).json()
    r = await client.post("/api/v1/shipping/webhook", json={
        "awb": s["awb"], "current_status": "In Transit", "location": 302001, "activity": {"a": 1}})
    assert r.status_code == 200


# ---------- R2-I7: publishing invalidates the cached sitemap ----------
async def test_sitemap_reflects_publish_immediately(client):
    ah = auth(await login(client, "9999900001"))
    slug = f"fresh-{uuid.uuid4().hex[:6]}"
    await client.get("/sitemap.xml")  # warm the cache
    art = (await client.post("/api/v1/admin/content/articles", json={
        "slug": slug, "title": "t", "body_html": "<p>x</p>", "category": "stories"}, headers=ah)).json()
    await client.post(f"/api/v1/admin/content/articles/{art['id']}/publish", headers=ah)
    assert slug in (await client.get("/sitemap.xml")).text


# ---------- R2-I8: DB backstops exist ----------
async def test_integrity_backstops_exist():
    names = {r[0] for r in await _q("SELECT indexname FROM pg_indexes WHERE schemaname='public'")}
    for idx in ("uq_wallet_txn_one_charge_per_delivery", "uq_credit_one_charge_per_delivery",
                "uq_coupon_redemption_order", "uq_referral_one_per_referee", "uq_payments_one_open_per_order",
                "uq_users_email_verified", "ix_wallet_txn_ref", "ix_payments_ref"):
        assert idx in names, idx
    checks = {r[0] for r in await _q("SELECT conname FROM pg_constraint WHERE contype='c'")}
    assert {"ck_coupons_used_count", "ck_gift_cards_balance_nonneg", "ck_loyalty_points_nonneg"} <= checks


# ---------- R2-I9: the seed script refuses production ----------
async def test_seed_refuses_prod(monkeypatch):
    from app import seed
    from app.core.config import settings

    monkeypatch.setattr(settings, "ENV", "prod")
    with pytest.raises(SystemExit):
        await seed.main()
