"""Regression tests for the backend-gap closure the apps/website/admin need:
one-time-order GST invoice, live order tracking, the in-app notification inbox,
and retail (D2C) subscriptions with an auto-provisioned home shop.
"""
from conftest import auth, login


async def _pid(client, token, sku="GHEE-1KG") -> int:
    prods = (await client.get("/api/v1/products", headers=auth(token))).json()
    rows = prods["items"] if isinstance(prods, dict) else prods
    return next(p["id"] for p in rows if p["sku"] == sku)


async def _paid_order(client, token, pid, qty=2) -> int:
    """Create a one-time order and drive it to payment_status=paid (dev gateway)."""
    oid = (
        await client.post(
            "/api/v1/orders",
            json={"items": [{"product_id": pid, "qty": qty}], "address": "12 MG Road"},
            headers=auth(token),
        )
    ).json()["id"]
    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "order_payment", "order_id": oid},
        headers=auth(token),
    )
    goid = create.json()["gateway_order_id"]
    await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=auth(token),
    )
    return oid


# ---------- one-time-order GST invoice ----------
async def test_order_invoice_requires_paid(client):
    cust = await login(client, "9876543210")
    pid = await _pid(client, cust)
    oid = (
        await client.post(
            "/api/v1/orders",
            json={"items": [{"product_id": pid, "qty": 1}]},
            headers=auth(cust),
        )
    ).json()["id"]
    r = await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))
    assert r.status_code == 409  # unpaid


async def test_order_invoice_idempotent(client):
    cust = await login(client, "9876543210")
    pid = await _pid(client, cust)
    oid = await _paid_order(client, cust, pid, qty=2)

    r1 = await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))
    assert r1.status_code == 200, r1.text
    inv1 = r1.json()
    assert inv1["invoice_no"]
    # 2 x ₹500 ghee, GST-inclusive @12% intra-state: taxable 892.86, CGST = SGST = 53.57.
    assert inv1["total"] == 1000.0 and inv1["taxable_value"] == 892.86
    assert inv1["cgst"] == inv1["sgst"] == 53.57 and inv1["igst"] == 0.0

    # Second call returns the SAME invoice (idempotent), not a new number.
    r2 = await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))
    assert r2.status_code == 200
    assert r2.json()["invoice_no"] == inv1["invoice_no"]
    assert r2.json()["id"] == inv1["id"]


async def test_order_invoice_idor(client):
    cust = await login(client, "9876543210")
    other = await login(client, "9191919191")
    pid = await _pid(client, cust)
    oid = await _paid_order(client, cust, pid)
    r = await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(other))
    assert r.status_code == 404  # not your order


# ---------- live order tracking ----------
async def test_order_tracking(client):
    cust = await login(client, "9876543210")
    pid = await _pid(client, cust)
    oid = await _paid_order(client, cust, pid)
    r = await client.get(f"/api/v1/orders/{oid}/tracking", headers=auth(cust))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order_id"] == oid
    assert body["payment_status"] == "paid"
    assert isinstance(body["shipments"], list)  # none yet until a shipment is created


async def test_order_tracking_idor(client):
    cust = await login(client, "9876543210")
    other = await login(client, "9191919191")
    pid = await _pid(client, cust)
    oid = await _paid_order(client, cust, pid)
    r = await client.get(f"/api/v1/orders/{oid}/tracking", headers=auth(other))
    assert r.status_code == 404


# ---------- in-app notification inbox ----------
async def test_notification_inbox(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    cid = (await client.get("/api/v1/profile", headers=auth(cust))).json()["user_id"]

    # shipment.delivered is transactional and targets the push channel, so a push row
    # lands in the customer's inbox (status may be skipped/no_address — still visible).
    await client.post(
        "/api/v1/admin/notifications/test",
        json={"user_id": cid, "event_type": "shipment.delivered", "data": {"awb": "X1"}},
        headers=auth(admin),
    )

    lst = await client.get("/api/v1/notifications", headers=auth(cust))
    assert lst.status_code == 200, lst.text
    items = lst.json()
    assert items, "expected at least one push notification in the inbox"
    assert all(i["channel"] == "push" for i in items)
    nid = items[0]["id"]

    before = (await client.get("/api/v1/notifications/unread-count", headers=auth(cust))).json()
    assert before["unread"] >= 1

    # IDOR: another user cannot read/mark my notification.
    other = await login(client, "9191919191")
    assert (
        await client.post(f"/api/v1/notifications/{nid}/read", headers=auth(other))
    ).status_code == 404

    marked = await client.post(f"/api/v1/notifications/{nid}/read", headers=auth(cust))
    assert marked.status_code == 200
    assert marked.json()["read_at"] is not None

    # read-all drives unread to zero.
    allr = await client.post("/api/v1/notifications/read-all", headers=auth(cust))
    assert allr.json()["unread"] == 0
    after = (await client.get("/api/v1/notifications/unread-count", headers=auth(cust))).json()
    assert after["unread"] == 0

    # unread_only filter now returns nothing.
    unread_only = await client.get(
        "/api/v1/notifications?unread_only=true", headers=auth(cust)
    )
    assert unread_only.json() == []


# ---------- retail (D2C) subscription with auto home shop ----------
async def test_retail_subscription_auto_home_shop(client):
    cust = await login(client, "9876543210")
    milk = await _pid(client, cust, sku="MILK-1L")

    # No shop_id: the customer's home shop is auto-provisioned.
    r = await client.post(
        "/api/v1/subscriptions",
        json={"home_name": "My Flat", "home_address": "12 MG Road", "home_pincode": "302001"},
        headers=auth(cust),
    )
    assert r.status_code == 200, r.text
    sub = r.json()
    sid, shop_id = sub["id"], sub["shop_id"]
    assert sub["status"] == "active"

    # Set a standing daily line.
    items = await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": milk, "default_qty": 1, "frequency": "daily"}],
        headers=auth(cust),
    )
    assert items.status_code == 200
    assert any(i["product_id"] == milk for i in items.json()["items"])

    # Idempotent: a second create reuses the same home shop (one home per account).
    r2 = await client.post("/api/v1/subscriptions", json={}, headers=auth(cust))
    assert r2.status_code == 200
    assert r2.json()["shop_id"] == shop_id

    # The customer can read back only their own subscription (scoped).
    got = await client.get(f"/api/v1/subscriptions/{sid}", headers=auth(cust))
    assert got.status_code == 200
    other = await login(client, "9191919191")
    assert (
        await client.get(f"/api/v1/subscriptions/{sid}", headers=auth(other))
    ).status_code == 404
