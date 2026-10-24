"""Tests for the Tier-1 audit hardening fixes."""
from conftest import auth, idem, login


async def _setup_delivery(client, h, tag):
    """Fresh product+tier+shop+subscription, run cutoff, return a delivery order."""
    pid = (await client.post("/api/v1/products", json={"sku": f"T1-DEL-{tag}", "name": "T1 Del"}, headers=h)).json()["id"]
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": f"T1T{tag}", "name": "t"}, headers=h)).json()["id"]
    await client.post("/api/v1/admin/product-prices", json={"product_id": pid, "price_tier_id": tid, "unit_price": 10, "valid_from": "2025-01-01"}, headers=h)
    shop_id = (await client.post("/api/v1/shops", json={"name": "T1 Shop"}, headers=h)).json()["id"]
    await client.post(f"/api/v1/admin/shops/{shop_id}/tier", json={"price_tier_id": tid}, headers=h)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=h)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items", json=[{"product_id": pid, "default_qty": 5, "frequency": "daily"}], headers=h)
    await client.post("/api/v1/admin/stock/batches", json={"product_id": pid, "batch_no": "T1B", "qty": 100, "expiry_date": "2040-01-01"}, headers=idem(h))
    date = "2039-07-07"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=h)).json()
    order = next(o for o in orders if o["shop_id"] == shop_id)
    return pid, order


async def test_deliver_twice_is_conflict(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid, order = await _setup_delivery(client, h, "twice")
    body = {"items": [{"product_id": pid, "qty_delivered": 5}], "returns": []}
    first = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json=body, headers=h)
    assert first.status_code == 200 and first.json()["status"] == "delivered"
    # Second submission must not silently overwrite a terminal order.
    second = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json=body, headers=h)
    assert second.status_code == 409


async def test_deliver_negative_qty_rejected(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid, order = await _setup_delivery(client, h, "neg")
    r = await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={"items": [{"product_id": pid, "qty_delivered": -3}], "returns": []},
        headers=h,
    )
    assert r.status_code == 422


async def test_ai_daily_cap_enforced(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AI_DAILY_CALLS_PER_USER", 2)
    cust = await login(client, "9876543210")
    hc = auth(cust)
    ok1 = await client.post("/api/v1/ai/support", json={"message": "hi"}, headers=hc)
    ok2 = await client.post("/api/v1/ai/support", json={"message": "hi"}, headers=hc)
    capped = await client.post("/api/v1/ai/support", json={"message": "hi"}, headers=hc)
    assert ok1.status_code == 200 and ok2.status_code == 200
    assert capped.status_code == 429


async def test_qty_check_constraint_blocks_negative_order_item(client):
    """DB-level CHECK: a negative order-item qty is rejected by Postgres."""
    import pytest
    import sqlalchemy as sa

    from app.core.db import engine

    async with engine.begin() as conn:
        pid = (await conn.execute(sa.text("SELECT id FROM products LIMIT 1"))).scalar()
        acct = (await conn.execute(sa.text("SELECT id FROM customer_accounts LIMIT 1"))).scalar()
        shop = (await conn.execute(sa.text("SELECT id FROM shops LIMIT 1"))).scalar()
        await conn.execute(
            sa.text(
                "INSERT INTO orders (order_no, account_id, shop_id, order_type, status, "
                "subtotal, discount, total, payment_status, created_at, updated_at) "
                "VALUES ('T1CHK', :a, :s, 'one_time', 'created', 0, 0, 0, 'pending', now(), now())"
            ),
            {"a": acct, "s": shop},
        )
        oid = (await conn.execute(sa.text("SELECT id FROM orders WHERE order_no='T1CHK'"))).scalar()

    # Bad insert runs in its own transaction so its failure rolls back cleanly.
    with pytest.raises(Exception) as ei:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO order_items (order_id, product_id, qty, unit_price, line_total) "
                    "VALUES (:o, :p, -1, 0, 0)"
                ),
                {"o": oid, "p": pid},
            )
    assert "ck_order_items_qty_pos" in str(ei.value) or "check" in str(ei.value).lower()

    async with engine.begin() as conn:  # cleanup
        await conn.execute(sa.text("DELETE FROM orders WHERE id = :o"), {"o": oid})
