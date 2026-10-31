"""Production-readiness policies: negative-stock control (ALLOW_NEGATIVE_STOCK) and the
admin exemption from the global per-IP rate limit."""
import uuid

import pytest
import sqlalchemy as sa
from conftest import auth, idem, login

from app.core.db import SessionLocal, engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _stop_without_stock(client, h, date, *, stock: float = 0):
    """Fresh product + tier + shop + standing order (qty 5) and a cutoff run for `date`;
    optionally receive `stock` units first. Returns (product_id, delivery order)."""
    tag = uuid.uuid4().hex[:8]
    pid = (await client.post("/api/v1/products", json={"sku": f"NEG-{tag}", "name": "neg"},
                             headers=h)).json()["id"]
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": f"NEG{tag}", "name": "t"},
                             headers=h)).json()["id"]
    await client.post("/api/v1/admin/product-prices", json={
        "product_id": pid, "price_tier_id": tid, "unit_price": 10, "valid_from": "2025-01-01"}, headers=h)
    shop = (await client.post("/api/v1/shops", json={"name": f"Neg {tag}"}, headers=h)).json()["id"]
    await client.post(f"/api/v1/admin/shops/{shop}/tier", json={"price_tier_id": tid}, headers=h)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=h)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": pid, "default_qty": 5, "frequency": "daily"}], headers=h)
    if stock:
        await client.post("/api/v1/admin/stock/batches", json={
            "product_id": pid, "batch_no": f"B{tag}", "qty": stock, "expiry_date": "2045-01-01"},
            headers=idem(h))
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=h)).json()
    return pid, next(o for o in orders if o["shop_id"] == shop)


async def _on_hand(pid) -> float:
    rows = await _q("SELECT coalesce(sum(qty_on_hand), 0) FROM stock_items WHERE product_id=:p", p=pid)
    return float(rows[0][0])


# ---------- ALLOW_NEGATIVE_STOCK ----------
async def test_negative_stock_allowed_by_default_backorders(client):
    from app.core.config import settings

    assert settings.ALLOW_NEGATIVE_STOCK is True  # default keeps the legacy behaviour
    h = auth(await login(client, "9999900001"))
    pid, order = await _stop_without_stock(client, h, "2042-01-01", stock=2)
    r = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver",
                          json={"items": [{"product_id": pid, "qty_delivered": 5}]}, headers=h)
    assert r.status_code == 200, r.text
    assert await _on_hand(pid) == -3.0  # silent backorder


async def test_negative_stock_refused_when_disabled(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ALLOW_NEGATIVE_STOCK", False)
    h = auth(await login(client, "9999900001"))
    pid, order = await _stop_without_stock(client, h, "2042-01-02", stock=2)
    r = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver",
                          json={"items": [{"product_id": pid, "qty_delivered": 5}]}, headers=h)
    assert r.status_code == 409
    assert "Not enough stock" in r.json()["detail"] and "2 on hand, 5 needed" in r.json()["detail"]
    # Nothing was applied: stock, order status and billing are untouched.
    assert await _on_hand(pid) == 2.0
    assert (await _q("SELECT status FROM delivery_orders WHERE id=:i", i=order["id"]))[0][0] in (
        "generated", "frozen")
    assert not await _q("SELECT 1 FROM wallet_transactions WHERE ref_type='delivery_order' AND ref_id=:i",
                        i=order["id"])
    # Within stock it still delivers, and on-hand never goes below zero.
    ok = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver",
                           json={"items": [{"product_id": pid, "qty_delivered": 2}]}, headers=h)
    assert ok.status_code == 200, ok.text
    assert await _on_hand(pid) == 0.0


async def test_consume_and_adjust_respect_the_setting(client, monkeypatch):
    from app.core.config import settings
    from app.services import batches, inventory

    h = auth(await login(client, "9999900001"))
    pid = (await client.post("/api/v1/products", json={"sku": f"NEGU-{uuid.uuid4().hex[:8]}", "name": "u"},
                             headers=h)).json()["id"]
    monkeypatch.setattr(settings, "ALLOW_NEGATIVE_STOCK", False)
    async with SessionLocal() as db:
        with pytest.raises(batches.InsufficientStock) as exc:
            await batches.consume_fefo(db, pid, 1, "delivery")
        assert exc.value.product_id == pid and exc.value.available == 0
        with pytest.raises(batches.InsufficientStock):
            await inventory.adjust_stock(db, pid, -1, "correction")
        await db.rollback()
    monkeypatch.setattr(settings, "ALLOW_NEGATIVE_STOCK", True)
    async with SessionLocal() as db:
        await batches.consume_fefo(db, pid, 1, "delivery")  # allowed: goes negative
        item = await inventory.adjust_stock(db, pid, -1, "correction")
        assert float(item.qty_on_hand) == -2.0
        await db.rollback()


# ---------- admin exemption from the global per-IP rate limit ----------
async def _tight_rate_limit(monkeypatch) -> None:
    """Turn the limiter on (3/min) with a clean window — AFTER the test has logged in.
    The window is pinned: with the real clock, a run that crossed a minute boundary
    landed in a fresh fixed window and the expected 429s never came (CI flake)."""
    import types

    from app.core import ratelimit
    from app.core.config import settings
    from app.core.redis import redis_client

    monkeypatch.setattr(ratelimit, "time", types.SimpleNamespace(time=lambda: 1_800_000_000.0))
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 3)
    await redis_client.flushdb()


async def test_admin_requests_are_not_rate_limited(client, monkeypatch):
    admin = auth(await login(client, "9999900001"))
    cust = auth(await login(client, "9000001401"))
    await _tight_rate_limit(monkeypatch)
    # Anonymous / customer traffic from the IP is limited...
    codes = [(await client.get("/api/v1/products", headers=cust)).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    assert (await client.get("/api/v1/products")).status_code == 429  # anonymous too
    # ...but an admin on the same IP is not throttled, however many calls it makes.
    for _ in range(10):
        assert (await client.get("/api/v1/admin/users", headers=admin)).status_code == 200


async def test_stale_or_forged_admin_token_gets_no_exemption(client, monkeypatch):
    from app.core.redis import redis_client
    from app.core.security import create_access_token

    await login(client, "9000001402")
    admin = await login(client, "9999900001")
    uid = (await _q("SELECT id FROM users WHERE phone='9000001402'"))[0][0]
    ver = (await _q("SELECT token_version FROM users WHERE id=:i", i=uid))[0][0]
    # A validly signed token that CLAIMS admin for a non-admin user is counted normally.
    fake = {"Authorization": f"Bearer {create_access_token(str(uid), 'admin', ver)}"}
    await _tight_rate_limit(monkeypatch)
    codes = [(await client.get("/api/v1/products", headers=fake)).status_code for _ in range(4)]
    assert codes[-1] == 429
    # A real admin token revoked by a token-version bump loses the exemption too.
    await _q("UPDATE users SET token_version = token_version + 1 WHERE phone='9999900001'")
    try:
        await redis_client.flushdb()
        codes = [(await client.get("/api/v1/products", headers=auth(admin))).status_code for _ in range(4)]
        assert codes[-1] == 429
    finally:
        await _q("UPDATE users SET token_version = token_version - 1 WHERE phone='9999900001'")
