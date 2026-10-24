from conftest import auth, login


async def _milk_id(client, h) -> int:
    prods = (await client.get("/api/v1/products", headers=h)).json()
    return next(p["id"] for p in prods if p["sku"] == "MILK-1L")


async def test_add_update_remove_item(client):
    cust = await login(client, "9100000701")  # isolated cart (shared customer leaked state)
    h = auth(cust)
    pid = await _milk_id(client, h)

    r = await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 2}, headers=h)
    assert r.status_code == 200, r.text
    cart = r.json()
    assert len(cart["items"]) == 1
    line = cart["items"][0]
    assert line["qty"] == 2.0 and line["unit_price"] == 27.0 and line["line_total"] == 54.0
    assert cart["subtotal"] == 54.0
    item_id = line["id"]

    # Adding the same product merges into the one line.
    r2 = await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 1}, headers=h)
    assert r2.json()["items"][0]["qty"] == 3.0

    # Update qty.
    r3 = await client.patch(f"/api/v1/cart/items/{item_id}", json={"qty": 5}, headers=h)
    assert r3.json()["items"][0]["qty"] == 5.0 and r3.json()["subtotal"] == 135.0

    # Remove.
    r4 = await client.delete(f"/api/v1/cart/items/{item_id}", headers=h)
    assert r4.status_code == 200 and r4.json()["items"] == []


async def test_coupon_preview_does_not_consume(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9100000702")  # isolated cart (shared customer leaked state)
    hc = auth(cust)
    pid = await _milk_id(client, hc)
    # Flat ₹5 coupon, one use per user.
    await client.post(
        "/api/v1/admin/coupons",
        json={"code": "CARTPREV", "discount_type": "flat", "value": 5, "per_user_limit": 1},
        headers=auth(admin),
    )
    await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 2}, headers=hc)

    # Previewing twice must not consume the single allowed use.
    p1 = await client.post("/api/v1/cart/coupon-preview", json={"code": "CARTPREV"}, headers=hc)
    p2 = await client.post("/api/v1/cart/coupon-preview", json={"code": "CARTPREV"}, headers=hc)
    assert p1.status_code == 200 and p1.json()["discount"] == 5.0
    assert p1.json()["final_total"] == 49.0
    assert p2.json()["discount"] == 5.0  # still available

    # And checkout can still actually use it.
    out = await client.post("/api/v1/cart/checkout", json={"coupon_code": "CARTPREV"}, headers=hc)
    assert out.status_code == 200 and out.json()["discount"] == 5.0
    assert out.json()["total"] == 49.0


async def test_checkout_converts_cart(client):
    cust = await login(client, "9100000703")  # isolated cart (shared customer leaked state)
    h = auth(cust)
    pid = await _milk_id(client, h)
    await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 3}, headers=h)

    out = await client.post("/api/v1/cart/checkout", json={"address": "9 Lake View, Udaipur"}, headers=h)
    assert out.status_code == 200, out.text
    order = out.json()
    assert order["order_type"] == "one_time" and order["total"] == 81.0
    assert any(i["product_id"] == pid and i["qty"] == 3.0 for i in order["items"])

    # Active cart is now fresh/empty (previous one converted).
    cart = (await client.get("/api/v1/cart", headers=h)).json()
    assert cart["items"] == []


async def test_checkout_empty_cart_422(client):
    cust = await login(client, "9100000777")  # fresh customer, empty cart
    r = await client.post("/api/v1/cart/checkout", json={}, headers=auth(cust))
    assert r.status_code == 422


async def test_cart_item_idor_404(client):
    a = await login(client, "9100000704")  # isolated cart (shared customer leaked state)
    pid = await _milk_id(client, auth(a))
    cart_a = (await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 1}, headers=auth(a))).json()
    a_item = cart_a["items"][0]["id"]

    b = await login(client, "9100000888")  # different customer
    # B cannot touch A's cart item — it isn't in B's cart.
    assert (await client.patch(f"/api/v1/cart/items/{a_item}", json={"qty": 9}, headers=auth(b))).status_code == 404
    assert (await client.delete(f"/api/v1/cart/items/{a_item}", headers=auth(b))).status_code == 404


async def test_concurrent_first_cart_touch_no_500(client):
    """#70: two concurrent first-touches on the active cart must not 500 on the partial
    unique index — the get-or-create collision is caught and re-selected."""
    import asyncio

    cust = await login(client, "9000000654")  # a user with no cart yet
    h = auth(cust)
    pid = await _milk_id(client, h)
    results = await asyncio.gather(
        client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 1}, headers=h),
        client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 1}, headers=h),
        return_exceptions=True,
    )
    codes = [r.status_code for r in results if not isinstance(r, Exception)]
    assert all(c == 200 for c in codes), [getattr(r, "text", r) for r in results]
    # Exactly one active cart exists afterward (the view reflects a single cart).
    view = (await client.get("/api/v1/cart", headers=h)).json()
    assert view["items"][0]["product_id"] == pid
