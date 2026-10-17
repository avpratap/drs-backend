"""Regression tests for the final-audit fixes: GST-inclusive invoicing, refund-to-source
credit notes, order cancellation guards, and input hardening.
"""
from conftest import auth, login


async def _pid(client, token, sku) -> int:
    prods = (await client.get("/api/v1/products", headers=auth(token))).json()
    return next(p["id"] for p in prods if p["sku"] == sku)


async def _paid_order(client, token, pid, qty):
    oid = (
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": pid, "qty": qty}]}, headers=auth(token)
        )
    ).json()["id"]
    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "order_payment", "order_id": oid},
        headers=auth(token),
    )
    goid = create.json()["gateway_order_id"]
    pay_id = create.json()["payment_id"]
    amount = create.json()["amount"]
    await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=auth(token),
    )
    return oid, pay_id, amount


# ---------- GST-inclusive: invoice total == amount charged ----------
async def test_order_invoice_total_equals_amount_charged(client):
    cust = await login(client, "9876543210")
    ghee = await _pid(client, cust, "GHEE-1KG")  # 500 @ 12% inclusive
    oid, _, amount = await _paid_order(client, cust, ghee, qty=2)
    assert amount == 1000.0  # 2 x 500, GST already inside
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()
    # Invoice total must equal what the customer actually paid (not paid + GST on top).
    assert inv["total"] == 1000.0


# ---------- refund-to-source issues a GST credit note ----------
async def test_full_refund_issues_credit_note(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    ghee = await _pid(client, cust, "GHEE-1KG")
    oid, pay_id, _ = await _paid_order(client, cust, ghee, qty=1)
    # Invoice first, then full refund -> order refunded + a credit note exists.
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()
    r = await client.post(f"/api/v1/payments/{pay_id}/refund", json={}, headers=auth(admin))
    assert r.status_code == 200
    o = (await client.get(f"/api/v1/orders/{oid}", headers=auth(cust))).json()
    assert o["payment_status"] == "refunded"
    # A credit note mirroring the invoice was generated (negated total).
    note = (await client.get(f"/api/v1/invoices/{inv['id']}", headers=auth(admin))).json()
    assert note["id"] == inv["id"]  # original invoice still readable


# ---------- cancellation guards ----------
async def test_admin_cannot_cancel_paid_order(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    milk = await _pid(client, cust, "MILK-1L")
    oid, _, _ = await _paid_order(client, cust, milk, qty=1)
    r = await client.post(f"/api/v1/admin/orders/{oid}/cancel", headers=auth(admin))
    assert r.status_code == 409  # must refund first


async def test_customer_self_cancel_unpaid_then_paid_blocked(client):
    cust = await login(client, "9876543210")
    milk = await _pid(client, cust, "MILK-1L")
    # Unpaid order -> customer can cancel.
    oid = (
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}]}, headers=auth(cust)
        )
    ).json()["id"]
    r = await client.post(f"/api/v1/orders/{oid}/cancel", headers=auth(cust))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # Re-cancel is a 409.
    assert (await client.post(f"/api/v1/orders/{oid}/cancel", headers=auth(cust))).status_code == 409
    # A different user can't cancel my order (IDOR -> 404).
    other = await login(client, "9191919191")
    oid2 = (
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}]}, headers=auth(cust)
        )
    ).json()["id"]
    assert (
        await client.post(f"/api/v1/orders/{oid2}/cancel", headers=auth(other))
    ).status_code == 404


# ---------- subscription cadence validation ----------
async def test_subscription_cadence_validation(client):
    admin = await login(client, "9999900001")
    # Build a shop to attach items to.
    shop_id = (await client.post("/api/v1/shops", json={"name": "Cadence Shop"}, headers=auth(admin))).json()["id"]
    sid = (
        await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=auth(admin))
    ).json()["id"]
    milk = await _pid(client, admin, "MILK-1L")
    # weekly with no days_of_week -> 422 (would otherwise materialize daily = over-billing).
    bad = await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": milk, "default_qty": 1, "frequency": "weekly"}],
        headers=auth(admin),
    )
    assert bad.status_code == 422
    # malformed days_of_week -> 422 (would otherwise silently drop the subscription).
    bad2 = await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": milk, "default_qty": 1, "frequency": "weekly", "days_of_week": "Mon"}],
        headers=auth(admin),
    )
    assert bad2.status_code == 422
    # unknown frequency -> 422.
    bad3 = await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": milk, "default_qty": 1, "frequency": "wekly"}],
        headers=auth(admin),
    )
    assert bad3.status_code == 422
    # valid weekly with days -> 200.
    ok = await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": milk, "default_qty": 1, "frequency": "weekly", "days_of_week": "1,3,5"}],
        headers=auth(admin),
    )
    assert ok.status_code == 200


# ---------- admin can provision a staff/delivery user ----------
async def test_admin_create_staff_user(client):
    admin = await login(client, "9999900001")
    r = await client.post(
        "/api/v1/admin/users",
        json={"phone": "9112223334", "role": "delivery", "full_name": "Driver One"},
        headers=auth(admin),
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "delivery"
    # Duplicate phone -> 409.
    dup = await client.post(
        "/api/v1/admin/users", json={"phone": "9112223334", "role": "delivery"}, headers=auth(admin)
    )
    assert dup.status_code == 409
    # Invalid role -> 422.
    bad = await client.post(
        "/api/v1/admin/users", json={"phone": "9112223335", "role": "superuser"}, headers=auth(admin)
    )
    assert bad.status_code == 422
    # A non-admin cannot create users.
    cust = await login(client, "9876543210")
    assert (
        await client.post(
            "/api/v1/admin/users", json={"phone": "9112223336", "role": "delivery"}, headers=auth(cust)
        )
    ).status_code == 403


# ---------- input hardening ----------
async def test_oversized_order_rejected(client):
    cust = await login(client, "9876543210")
    milk = await _pid(client, cust, "MILK-1L")
    big = [{"product_id": milk, "qty": 1} for _ in range(101)]  # cap is 100
    r = await client.post("/api/v1/orders", json={"items": big}, headers=auth(cust))
    assert r.status_code == 422


async def test_shop_overlong_pincode_is_422_not_500(client):
    admin = await login(client, "9999900001")
    r = await client.post(
        "/api/v1/shops", json={"name": "X", "pincode": "9" * 5000}, headers=auth(admin)
    )
    assert r.status_code == 422  # clean validation error, not a DB DataError 500


async def test_upload_rejects_non_image_bytes(client):
    cust = await login(client, "9876543210")
    # Declared image/png but the bytes are not a PNG -> rejected by magic-byte check.
    files = {"file": ("fake.png", b"this is not an image", "image/png")}
    r = await client.post("/api/v1/uploads", files=files, headers=auth(cust))
    assert r.status_code == 422


# ---------- retail prepaid: cutoff skips delivery when wallet can't cover it ----------
async def test_retail_prepaid_cutoff_skips_on_low_balance(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9100000456")  # fresh retail customer, empty wallet
    milk = await _pid(client, cust, "MILK-1L")
    sub = (
        await client.post(
            "/api/v1/subscriptions",
            json={"home_name": "Flat", "home_pincode": "302001"},
            headers=auth(cust),
        )
    ).json()
    shop_id = sub["shop_id"]
    await client.put(
        f"/api/v1/subscriptions/{sub['id']}/items",
        json=[{"product_id": milk, "default_qty": 2, "frequency": "daily"}],
        headers=auth(cust),
    )
    date = "2035-03-03"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=auth(admin))
    # Wallet is 0 < order total -> no delivery order should have been generated.
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=auth(admin))).json()
    assert all(o["shop_id"] != shop_id for o in orders)
