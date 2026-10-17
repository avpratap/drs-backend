from conftest import auth, login


async def _paid_order(client, ch):
    prods = (await client.get("/api/v1/products", headers=ch)).json()
    pid = next(p["id"] for p in prods if p["sku"] == "MILK-1L")
    oid = (await client.post(
        "/api/v1/orders", json={"items": [{"product_id": pid, "qty": 2}], "address": "x"}, headers=ch
    )).json()["id"]
    created = (await client.post(
        "/api/v1/payments/create", json={"purpose": "order_payment", "order_id": oid}, headers=ch
    )).json()
    v = await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": created["gateway_order_id"], "gateway_payment_id": "pay_test", "signature": "dev"},
        headers=ch,
    )
    assert v.status_code == 200
    return pid


async def test_analytics_overview_and_series(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    pid = await _paid_order(client, auth(cust))

    ov = await client.get("/api/v1/admin/analytics/overview", headers=ah)
    assert ov.status_code == 200
    body = ov.json()
    assert body["paid_order_revenue"] > 0 and body["orders"] >= 1
    assert "low_stock_items" in body and "credit_outstanding" in body

    rev = await client.get("/api/v1/admin/analytics/revenue?days=30", headers=ah)
    assert rev.status_code == 200 and isinstance(rev.json(), list) and len(rev.json()) >= 1
    assert rev.json()[0]["revenue"] >= 0

    top = await client.get("/api/v1/admin/analytics/top-products?limit=10&days=30", headers=ah)
    assert top.status_code == 200 and any(r["product_id"] == pid for r in top.json())


async def test_analytics_admin_gated(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/analytics/overview", headers=auth(cust))).status_code == 403


async def test_outbound_webhook_ops(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    hook = (await client.post(
        "/api/v1/admin/outbound-webhooks",
        json={"event_type": "order.paid", "url": "http://localhost/hook"},
        headers=ah,
    )).json()
    hid = hook["id"]
    # PATCH toggles active.
    p = await client.patch(f"/api/v1/admin/outbound-webhooks/{hid}", json={"active": False}, headers=ah)
    assert p.status_code == 200 and p.json()["active"] is False
    # Test-fire creates a delivery row (localhost short-circuits to sent).
    t = await client.post(f"/api/v1/admin/outbound-webhooks/{hid}/test", headers=ah)
    assert t.status_code == 200 and t.json()["status"] == "sent"
    dl = await client.get(f"/api/v1/admin/outbound-webhooks/{hid}/deliveries", headers=ah)
    assert dl.status_code == 200 and len(dl.json()) >= 1
    # Admin-gate.
    cust = await login(client, "9876543210")
    assert (await client.patch(f"/api/v1/admin/outbound-webhooks/{hid}", json={"active": True}, headers=auth(cust))).status_code == 403
