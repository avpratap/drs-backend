"""Regression tests for bugs found in the deep bug-hunt (keep them fixed)."""
from conftest import auth, idem, login


async def _product(client, h, sku, price=None, tier_code=None):
    pid = (await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)).json()["id"]
    if price is not None:
        tid = (await client.post("/api/v1/admin/price-tiers", json={"code": tier_code or sku[:8], "name": "t"}, headers=h)).json()["id"]
        await client.post(
            "/api/v1/admin/product-prices",
            json={"product_id": pid, "price_tier_id": tid, "unit_price": price, "valid_from": "2025-01-01"},
            headers=h,
        )
    return pid


async def test_order_unpriced_product_rejected(client):
    """BUG: a product with no configured price was ordered at ₹0 (free)."""
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    pid = await _product(client, auth(admin), "BUG-NOPRICE")  # no price configured
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": 2}], "address": "x"},
        headers=auth(cust),
    )
    assert r.status_code == 422


async def test_order_retired_product_rejected(client):
    """BUG: retired (soft-deleted) products were still orderable.

    The retired-status guard fires before pricing, so no price needed here."""
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    pid = await _product(client, ah, "BUG-RETIRED")  # no price
    await client.delete(f"/api/v1/products/{pid}", headers=ah)  # retire
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": 1}], "address": "x"},
        headers=auth(cust),
    )
    assert r.status_code == 422


async def _delivery_order(client, h, tag):
    pid = await _product(client, h, f"BUG-DEL-{tag}", price=10, tier_code=f"BUGT{tag}")
    shop_id = (await client.post("/api/v1/shops", json={"name": f"Bug Shop {tag}"}, headers=h)).json()["id"]
    tid = (await client.get("/api/v1/admin/price-tiers", headers=h)).json()
    tid = next(t["id"] for t in tid if t["code"] == f"BUGT{tag}")
    await client.post(f"/api/v1/admin/shops/{shop_id}/tier", json={"price_tier_id": tid}, headers=h)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=h)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items", json=[{"product_id": pid, "default_qty": 5, "frequency": "daily"}], headers=h)
    await client.post("/api/v1/admin/stock/batches", json={"product_id": pid, "batch_no": f"BB{tag}", "qty": 100, "expiry_date": "2041-01-01"}, headers=idem(h))
    date = "2039-08-08"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=h)).json()
    order = next(o for o in orders if o["shop_id"] == shop_id)
    # Cancel the subscription so it doesn't materialize into other tests' cutoff runs
    # (the already-generated order for `date` is unaffected).
    await client.post(f"/api/v1/subscriptions/{sid}/cancel", headers=h)
    return pid, order


async def test_return_exceeding_delivered_rejected(client):
    """BUG: returning more than delivered synthesized phantom stock + zeroed the bill."""
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid, order = await _delivery_order(client, h, "RET")
    r = await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={"items": [{"product_id": pid, "qty_delivered": 2}],
              "returns": [{"product_id": pid, "qty_returned": 10, "reason": "refused"}]},
        headers=h,
    )
    assert r.status_code == 422  # returned (10) exceeds delivered (2)


async def test_return_of_undelivered_product_rejected(client):
    """BUG: returning product B could cancel the charge for delivered product A."""
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid, order = await _delivery_order(client, h, "CROSS")
    other = await _product(client, h, "BUG-OTHER", price=10, tier_code="BUGOTH")
    r = await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={"items": [{"product_id": pid, "qty_delivered": 5}],
              "returns": [{"product_id": other, "qty_returned": 5, "reason": "refused"}]},
        headers=h,
    )
    assert r.status_code == 422  # return product not in this order


async def test_over_delivery_rejected(client):
    """BUG: delivering more than planned could over-bill beyond consent."""
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid, order = await _delivery_order(client, h, "OVER")  # planned qty 5
    r = await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={"items": [{"product_id": pid, "qty_delivered": 9}], "returns": []},
        headers=h,
    )
    assert r.status_code == 422  # 9 > planned 5


async def test_shipping_cancelled_order_rejected(client):
    """BUG: create_shipment only checked payment_status, shipping cancelled orders."""
    import sqlalchemy as sa

    from app.core.db import engine

    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    # Use the seeded, RETAIL-priced product so the order is created cleanly.
    prods = (await client.get("/api/v1/products", headers=auth(cust))).json()
    pid = next(p["id"] for p in prods if p["sku"] == "MILK-1L")
    oid = (await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": 1}], "address": "x"},
        headers=auth(cust),
    )).json()["id"]
    # Force the order into a cancelled state.
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE orders SET status='cancelled' WHERE id=:o"), {"o": oid})
    r = await client.post(
        "/api/v1/admin/shipments",
        json={"order_id": oid, "pincode": "302001", "weight_kg": 1.0},
        headers=ah,
    )
    assert r.status_code == 409
