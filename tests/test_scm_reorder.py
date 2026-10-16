from conftest import auth, login


async def _product(client, h, sku) -> int:
    return (await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)).json()["id"]


async def _location(client, h, name) -> int:
    return (await client.post("/api/v1/admin/locations", json={"name": name, "kind": "warehouse"}, headers=h)).json()["id"]


async def test_reorder_suggestion_tops_up_to_2x(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "REORD-A")
    loc = await _location(client, h, "Reorder WH A")
    # reorder point 20, bring on-hand to 5 (below point) -> suggest 2*20 - 5 = 35.
    await client.post("/api/v1/admin/stock/reorder", json={"product_id": pid, "reorder_point": 20, "location_id": loc}, headers=h)
    await client.post("/api/v1/admin/stock/adjust", json={"product_id": pid, "change": 5, "reason": "receipt", "location_id": loc}, headers=h)

    rows = (await client.get("/api/v1/admin/stock/reorder-suggestions", headers=h)).json()
    mine = next(r for r in rows if r["product_id"] == pid and r["location_id"] == loc)
    assert mine["qty_on_hand"] == 5.0
    assert mine["reorder_point"] == 20.0
    assert mine["suggested_qty"] == 35.0


async def test_reorder_suggestion_excludes_well_stocked(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "REORD-B")
    loc = await _location(client, h, "Reorder WH B")
    await client.post("/api/v1/admin/stock/reorder", json={"product_id": pid, "reorder_point": 5, "location_id": loc}, headers=h)
    await client.post("/api/v1/admin/stock/adjust", json={"product_id": pid, "change": 100, "reason": "receipt", "location_id": loc}, headers=h)
    rows = (await client.get("/api/v1/admin/stock/reorder-suggestions", headers=h)).json()
    assert all(not (r["product_id"] == pid and r["location_id"] == loc) for r in rows)


async def test_reorder_suggestions_requires_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/stock/reorder-suggestions", headers=auth(cust))).status_code == 403
