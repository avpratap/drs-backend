from conftest import auth, login


async def test_location_crud_single_default(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    a = await client.post("/api/v1/admin/locations", json={"name": "Jaipur Hub", "kind": "hub", "is_default": True}, headers=h)
    assert a.status_code == 200
    b = await client.post("/api/v1/admin/locations", json={"name": "Cold Store", "kind": "cold_storage", "is_default": True}, headers=h)
    assert b.status_code == 200
    locs = (await client.get("/api/v1/admin/locations", headers=h)).json()
    assert sum(1 for loc in locs if loc["is_default"]) == 1  # only one default
    assert next(loc for loc in locs if loc["is_default"])["id"] == b.json()["id"]


async def test_reorder_point_and_low_stock(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    # Set reorder point 50 for product 1, stock it to 40 -> appears in low list.
    await client.post("/api/v1/admin/stock/reorder", json={"product_id": 1, "reorder_point": 50}, headers=h)
    await client.post("/api/v1/admin/stock/adjust", json={"product_id": 1, "change": 40, "reason": "receipt"}, headers=h)
    low = (await client.get("/api/v1/admin/stock/low", headers=h)).json()
    assert any(s["product_id"] == 1 for s in low)
    # Receive more -> above reorder -> drops off the low list.
    await client.post("/api/v1/admin/stock/adjust", json={"product_id": 1, "change": 100, "reason": "receipt"}, headers=h)
    low2 = (await client.get("/api/v1/admin/stock/low", headers=h)).json()
    assert all(s["product_id"] != 1 for s in low2)


async def _stock(client, h, pid) -> float:
    rows = (await client.get("/api/v1/admin/stock", headers=h)).json()
    return next((s["qty_on_hand"] for s in rows if s["product_id"] == pid), 0.0)


async def test_delivery_decrements_stock_idempotently(client):
    admin = await login(client, "9999900001")
    h = auth(admin)  # admin sees all shops and can mark deliveries
    date = "2038-08-08"

    # Fully isolated: own product + price + shop + subscription so other tests'
    # subscriptions/stock can't perturb this one.
    pid = (await client.post("/api/v1/products", json={"sku": "SCM-DLV-1", "name": "SCM Milk"}, headers=h)).json()["id"]
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": "SCMDLV", "name": "t"}, headers=h)).json()["id"]
    await client.post("/api/v1/admin/product-prices", json={"product_id": pid, "price_tier_id": tid, "unit_price": 10, "valid_from": "2025-01-01"}, headers=h)
    shop_id = (await client.post("/api/v1/shops", json={"name": "SCM Shop"}, headers=h)).json()["id"]
    await client.post(f"/api/v1/admin/shops/{shop_id}/tier", json={"price_tier_id": tid}, headers=h)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=h)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items", json=[{"product_id": pid, "default_qty": 7, "frequency": "daily"}], headers=h)
    await client.post("/api/v1/admin/stock/adjust", json={"product_id": pid, "change": 100, "reason": "receipt"}, headers=h)

    before = await _stock(client, h, pid)
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=h)).json()
    order = next(o for o in orders if o["shop_id"] == shop_id)
    item = order["items"][0]
    body = {"items": [{"product_id": pid, "qty_delivered": item["qty_planned"]}], "returns": []}
    await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json=body, headers=h)

    after = await _stock(client, h, pid)
    assert round(before - after, 3) == 7.0  # dropped by delivered qty

    # Re-submit — stock must NOT drop again (idempotent per delivery_order).
    await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json=body, headers=h)
    assert await _stock(client, h, pid) == after
