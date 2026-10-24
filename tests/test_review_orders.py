"""Regression tests for the review's orders / cart / catalog fixes (O*), see
scratchpad/MASTER_FIXLIST.md. Each test uses its own fresh customer."""
import asyncio
import datetime as dt
import uuid

import sqlalchemy as sa
from conftest import auth, login

from app.core.db import engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _milk() -> int:
    return (await _q("SELECT id FROM products WHERE sku='MILK-1L'"))[0][0]


async def _admin(client):
    return auth(await login(client, "9999900001"))


async def _order(client, h, items, **extra):
    return await client.post("/api/v1/orders", json={"items": items, **extra}, headers=h)


async def _priced_product(client, ah, uom="litre", price=10) -> int:
    pid = (await client.post("/api/v1/products", json={
        "sku": f"O-{uuid.uuid4().hex[:10]}", "name": "o", "uom": uom}, headers=ah)).json()["id"]
    tier = (await _q("SELECT id FROM price_tiers WHERE code='RETAIL'"))[0][0]
    await _q("INSERT INTO product_prices (product_id, price_tier_id, unit_price, min_qty, valid_from) "
             "VALUES (:p, :t, :u, 1, '2025-01-01')", p=pid, t=tier, u=price)
    return pid


# ---------- O2/O3: no cancel once shipped; admin cancel guarded ----------
async def test_cancel_refused_once_order_has_a_live_shipment(client):
    ah = await _admin(client)
    ch = auth(await login(client, "9000000930"))
    o = (await _order(client, ch, [{"product_id": await _milk(), "qty": 1}])).json()
    s = await client.post("/api/v1/admin/shipments", json={"order_id": o["id"], "pincode": "302001", "cod": True},
                          headers=ah)
    assert s.status_code == 200, s.text
    assert (await client.post(f"/api/v1/orders/{o['id']}/cancel", headers=ch)).status_code == 409
    assert (await client.post(f"/api/v1/admin/orders/{o['id']}/cancel", headers=ah)).status_code == 409
    # Cancel the shipment first → the order can then be cancelled.
    assert (await client.post(f"/api/v1/admin/shipments/{s.json()['id']}/cancel", headers=ah)).status_code == 200
    assert (await client.post(f"/api/v1/orders/{o['id']}/cancel", headers=ch)).status_code == 200


async def test_admin_can_cancel_a_fully_refunded_order(client):
    ah = await _admin(client)
    ch = auth(await login(client, "9000000931"))
    o = (await _order(client, ch, [{"product_id": await _milk(), "qty": 1}])).json()
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "order_payment", "order_id": o["id"]},
                             headers=ch)).json()
    await client.post("/api/v1/payments/verify", json={
        "gateway_order_id": pay["gateway_order_id"], "gateway_payment_id": "p", "signature": "dev"}, headers=ch)
    # Paid → must refund first.
    assert (await client.post(f"/api/v1/admin/orders/{o['id']}/cancel", headers=ah)).status_code == 409
    assert (await client.post(f"/api/v1/payments/{pay['payment_id']}/refund", json={}, headers=ah)).status_code == 200
    r = await client.post(f"/api/v1/admin/orders/{o['id']}/cancel", headers=ah)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


# ---------- O4/O5: cart qty caps + concurrent adds ----------
async def test_cart_merged_qty_is_capped(client):
    ch = auth(await login(client, "9000000932"))
    milk = await _milk()
    assert (await client.post("/api/v1/cart/items", json={"product_id": milk, "qty": 60000}, headers=ch)).status_code == 200
    over = await client.post("/api/v1/cart/items", json={"product_id": milk, "qty": 60000}, headers=ch)
    assert over.status_code == 422
    cart = (await client.get("/api/v1/cart", headers=ch)).json()
    assert [i["qty"] for i in cart["items"]] == [60000.0]


async def test_concurrent_first_adds_make_one_line(client):
    ch = auth(await login(client, "9000000933"))
    await client.get("/api/v1/cart", headers=ch)  # create the cart first
    milk = await _milk()
    rs = await asyncio.gather(*[
        client.post("/api/v1/cart/items", json={"product_id": milk, "qty": 1}, headers=ch) for _ in range(4)
    ])
    assert all(r.status_code == 200 for r in rs), [r.text for r in rs]
    cart = (await client.get("/api/v1/cart", headers=ch)).json()
    assert len(cart["items"]) == 1 and cart["items"][0]["qty"] == 4.0


# ---------- O6: one outstanding unpaid reservation per coupon per account ----------
async def test_coupon_cannot_be_hoarded_by_unpaid_orders(client):
    ah = await _admin(client)
    ch = auth(await login(client, "9000000934"))
    await client.post("/api/v1/admin/coupons", json={
        "code": "HOARD934", "discount_type": "flat", "value": 5, "usage_limit": 100}, headers=ah)
    milk = await _milk()
    first = await _order(client, ch, [{"product_id": milk, "qty": 2}], coupon_code="HOARD934")
    assert first.status_code == 200
    second = await _order(client, ch, [{"product_id": milk, "qty": 2}], coupon_code="HOARD934")
    assert second.status_code == 409
    await client.post(f"/api/v1/orders/{first.json()['id']}/cancel", headers=ch)
    third = await _order(client, ch, [{"product_id": milk, "qty": 2}], coupon_code="HOARD934")
    assert third.status_code == 200


# ---------- O7: cart prices with the same tier as checkout ----------
async def test_cart_tier_matches_checkout_home_shop(client):
    ah = await _admin(client)
    sub = await login(client, "9000000935")
    ch = auth(sub)
    milk = await _milk()
    # This account's FIRST shop is a B2B store with a cheaper tier; checkout uses the home
    # shop (retail) — the cart must quote what checkout will charge.
    await client.get("/api/v1/cart", headers=ch)
    acct = (await _q("SELECT a.id FROM customer_accounts a JOIN users u ON u.id=a.owner_user_id "
                     "WHERE u.phone='9000000935'"))[0][0]
    await _q("INSERT INTO price_tiers (code, name, tier_type) VALUES ('B2B935', 'b2b', 'b2b')")
    tier = (await _q("SELECT id FROM price_tiers WHERE code='B2B935'"))[0][0]
    await _q("INSERT INTO product_prices (product_id, price_tier_id, unit_price, min_qty, valid_from) "
             "VALUES (:p, :t, 20, 1, '2025-01-01')", p=milk, t=tier)
    await _q("INSERT INTO shops (account_id, name, status, price_tier_id, is_home) "
             "VALUES (:a, 'Store 935', 'active', :t, false)", a=acct, t=tier)
    await client.post("/api/v1/cart/items", json={"product_id": milk, "qty": 2}, headers=ch)
    view = (await client.get("/api/v1/cart", headers=ch)).json()
    order = (await client.post("/api/v1/cart/checkout", json={}, headers=ch)).json()
    assert view["subtotal"] == order["subtotal"] == 54.0  # retail 27 x 2, not the store's 20
    assert ah


# ---------- O10: concurrent first orders share one home shop ----------
async def test_concurrent_first_orders_one_home_shop(client):
    ch = auth(await login(client, "9000000936"))
    milk = await _milk()
    rs = await asyncio.gather(*[_order(client, ch, [{"product_id": milk, "qty": 1}]) for _ in range(3)])
    assert all(r.status_code == 200 for r in rs), [r.text for r in rs]
    homes = await _q("SELECT count(*) FROM shops s JOIN customer_accounts a ON a.id=s.account_id "
                     "JOIN users u ON u.id=a.owner_user_id WHERE u.phone='9000000936' AND s.is_home")
    assert homes[0][0] == 1


# ---------- O11: cart checkout keeps the delivery state (GST place of supply) ----------
async def test_cart_checkout_captures_ship_state(client):
    ch = auth(await login(client, "9000000937"))
    await client.post("/api/v1/cart/items", json={"product_id": await _milk(), "qty": 1}, headers=ch)
    o = (await client.post("/api/v1/cart/checkout", json={"state": "Maharashtra", "pincode": "411001"},
                           headers=ch)).json()
    assert (await _q("SELECT ship_state FROM orders WHERE id=:i", i=o["id"]))[0][0] == "Maharashtra"


# ---------- O12/O13: merged qty cap + whole units ----------
async def test_order_merged_qty_cap_and_whole_units(client):
    ah = await _admin(client)
    ch = auth(await login(client, "9000000938"))
    milk = await _milk()
    over = await _order(client, ch, [{"product_id": milk, "qty": 60000}, {"product_id": milk, "qty": 60000}])
    assert over.status_code == 422
    piece = await _priced_product(client, ah, uom="piece")
    assert (await _order(client, ch, [{"product_id": piece, "qty": 1.5}])).status_code == 422
    ok = await _order(client, ch, [{"product_id": piece, "qty": 2}])
    assert ok.status_code == 200 and ok.json()["total"] == 20.0
    # Stored qty x unit price == line total (qty quantized before pricing).
    lit = await _order(client, ch, [{"product_id": milk, "qty": 1.0004}])
    line = lit.json()["items"][0]
    assert round(line["qty"] * line["unit_price"], 2) == line["line_total"]


# ---------- O14/O15: catalog input bounds; retired products ----------
async def test_product_gst_bounds_and_retired_products(client):
    ah = await _admin(client)
    ch = auth(await login(client, "9000000939"))
    for bad in (100, 150, -1):
        r = await client.post("/api/v1/products", json={"sku": f"G-{uuid.uuid4().hex[:8]}", "name": "x",
                                                        "gst_rate": bad}, headers=ah)
        assert r.status_code == 422
    assert (await client.post("/api/v1/products", json={"sku": f"P-{uuid.uuid4().hex[:8]}", "name": "x",
                                                        "pack_size": 0}, headers=ah)).status_code == 422
    pid = await _priced_product(client, ah)
    assert (await client.delete(f"/api/v1/products/{pid}", headers=ah)).status_code in (200, 204)
    assert (await client.post("/api/v1/wishlist", json={"product_id": pid}, headers=ch)).status_code == 404
    assert (await client.get(f"/api/v1/products/{pid}/variants")).status_code == 404
    assert (await client.post(f"/api/v1/products/{pid}/ratings", json={"stars": 1}, headers=ch)).status_code == 404
    assert dt
