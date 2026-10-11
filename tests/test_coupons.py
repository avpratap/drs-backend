from conftest import auth, login


async def _mk_coupon(client, admin_h, **over):
    body = {"code": over.pop("code", "SAVE10"), "discount_type": "percent", "value": 10}
    body.update(over)
    return await client.post("/api/v1/admin/coupons", json=body, headers=admin_h)


async def test_admin_coupon_crud(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    c = await _mk_coupon(client, h, code="WELCOME", value=15)
    assert c.status_code == 200 and c.json()["code"] == "WELCOME"
    # duplicate code
    assert (await _mk_coupon(client, h, code="WELCOME")).status_code == 409
    # percent > 100 rejected
    assert (await _mk_coupon(client, h, code="BIG", value=150)).status_code == 422
    listed = await client.get("/api/v1/admin/coupons?active=true", headers=h)
    assert any(x["code"] == "WELCOME" for x in listed.json())
    cid = c.json()["id"]
    off = await client.post(f"/api/v1/admin/coupons/{cid}/deactivate", headers=h)
    assert off.json()["active"] is False


async def test_coupon_requires_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/coupons", headers=auth(cust))).status_code == 403


async def test_percent_coupon_applies_to_order(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    await _mk_coupon(client, auth(admin), code="TEN", discount_type="percent", value=10)
    # 2 x 27 = 54 subtotal, 10% -> 5.4 discount -> total 48.6
    order = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": 1, "qty": 2}], "coupon_code": "TEN"},
        headers=auth(cust),
    )
    assert order.status_code == 200
    body = order.json()
    assert body["discount"] == 5.4 and body["total"] == 48.6 and body["coupon_code"] == "TEN"


async def test_flat_coupon_capped_and_min_order(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    h = auth(admin)
    # Flat ₹100 off but min order ₹500 -> rejected on a ₹54 order.
    await _mk_coupon(client, h, code="FLAT100", discount_type="flat", value=100, min_order_value=500)
    low = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": 1, "qty": 2}], "coupon_code": "FLAT100"},
        headers=auth(cust),
    )
    assert low.status_code == 422

    # Flat ₹100 off, no minimum, on ₹54 order -> discount capped to subtotal, total 0.
    await _mk_coupon(client, h, code="FLATBIG", discount_type="flat", value=100)
    ok = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": 1, "qty": 2}], "coupon_code": "FLATBIG"},
        headers=auth(cust),
    )
    assert ok.json()["discount"] == 54.0 and ok.json()["total"] == 0.0


async def test_invalid_and_usage_limited_coupons(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    h = auth(admin)
    # Unknown code
    bad = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": 1, "qty": 1}], "coupon_code": "NOPE"},
        headers=auth(cust),
    )
    assert bad.status_code == 422

    # usage_limit=1 -> second use blocked (409)
    await _mk_coupon(client, h, code="ONCE", discount_type="flat", value=5, usage_limit=1)
    first = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": 1, "qty": 1}], "coupon_code": "ONCE"},
        headers=auth(cust),
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": 1, "qty": 1}], "coupon_code": "ONCE"},
        headers=auth(cust),
    )
    assert second.status_code == 409
