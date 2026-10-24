from conftest import auth, idem, login


async def _product(client, h, sku) -> int:
    return (await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)).json()["id"]


async def _location(client, h, name) -> int:
    return (await client.post("/api/v1/admin/locations", json={"name": name, "kind": "hub"}, headers=h)).json()["id"]


async def _stock_at(client, h, pid, loc) -> float:
    rows = (await client.get(f"/api/v1/admin/stock?location_id={loc}&product_id={pid}", headers=h)).json()
    return next((s["qty_on_hand"] for s in rows if s["product_id"] == pid), 0.0)


async def _default_loc(client, h) -> int:
    locs = (await client.get("/api/v1/admin/locations", headers=h)).json()
    return next((x["id"] for x in locs if x["is_default"]), locs[0]["id"])


async def test_transfer_full_flow_preserves_lot(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "TR-FLOW-A")
    src = await _default_loc(client, h)
    dst = await _location(client, h, "TR Hub North")

    # Seed a batch at the source.
    await client.post(
        "/api/v1/admin/stock/batches",
        json={"product_id": pid, "batch_no": "LOT-TR1", "qty": 50, "location_id": src, "expiry_date": "2041-01-01"},
        headers=idem(h),
    )
    assert await _stock_at(client, h, pid, src) == 50.0
    assert await _stock_at(client, h, pid, dst) == 0.0

    tr = (await client.post(
        "/api/v1/admin/transfers",
        json={"to_location_id": dst, "items": [{"product_id": pid, "qty": 30}]},
        headers=h,
    )).json()
    assert tr["status"] == "draft" and tr["transfer_no"].startswith("TR")
    tid = tr["id"]

    # Cannot receive before dispatch.
    assert (await client.post(f"/api/v1/admin/transfers/{tid}/receive", headers=h)).status_code == 409

    disp = await client.post(f"/api/v1/admin/transfers/{tid}/dispatch", headers=h)
    assert disp.status_code == 200 and disp.json()["status"] == "in_transit"
    # Source decremented immediately; destination not yet credited (in transit).
    assert await _stock_at(client, h, pid, src) == 20.0
    assert await _stock_at(client, h, pid, dst) == 0.0

    rec = await client.post(f"/api/v1/admin/transfers/{tid}/receive", headers=h)
    assert rec.status_code == 200 and rec.json()["status"] == "received"
    assert await _stock_at(client, h, pid, dst) == 30.0
    # Lot identity travelled: same batch_no + expiry now exists at destination.
    dst_batches = (await client.get(f"/api/v1/admin/stock/batches?product_id={pid}&location_id={dst}&active_only=false", headers=h)).json()
    moved = next(b for b in dst_batches if b["batch_no"] == "LOT-TR1")
    assert moved["qty"] == 30.0 and moved["expiry_date"] == "2041-01-01"


async def test_transfer_insufficient_stock_422(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "TR-SHORT-A")
    src = await _default_loc(client, h)
    dst = await _location(client, h, "TR Hub Short")
    await client.post(
        "/api/v1/admin/stock/batches",
        json={"product_id": pid, "batch_no": "LOT-SHORT", "qty": 5, "location_id": src, "expiry_date": "2041-01-01"},
        headers=idem(h),
    )
    tid = (await client.post(
        "/api/v1/admin/transfers",
        json={"to_location_id": dst, "items": [{"product_id": pid, "qty": 999}]},
        headers=h,
    )).json()["id"]
    r = await client.post(f"/api/v1/admin/transfers/{tid}/dispatch", headers=h)
    assert r.status_code == 422
    # Still draft — nothing moved.
    assert (await client.get(f"/api/v1/admin/transfers/{tid}", headers=h)).json()["status"] == "draft"
    assert await _stock_at(client, h, pid, src) == 5.0


async def test_transfer_same_location_rejected(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "TR-SAME-A")
    src = await _default_loc(client, h)
    r = await client.post(
        "/api/v1/admin/transfers",
        json={"from_location_id": src, "to_location_id": src, "items": [{"product_id": pid, "qty": 1}]},
        headers=h,
    )
    assert r.status_code == 422


async def test_transfer_unknown_location_404(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "TR-BADLOC-A")
    r = await client.post(
        "/api/v1/admin/transfers",
        json={"to_location_id": 999999, "items": [{"product_id": pid, "qty": 1}]},
        headers=h,
    )
    assert r.status_code == 404


async def test_transfer_cancel_and_guards(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "TR-CANCEL-A")
    dst = await _location(client, h, "TR Hub Cancel")
    tid = (await client.post(
        "/api/v1/admin/transfers",
        json={"to_location_id": dst, "items": [{"product_id": pid, "qty": 1}]},
        headers=h,
    )).json()["id"]
    c = await client.post(f"/api/v1/admin/transfers/{tid}/cancel", headers=h)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    # Cancelled cannot be dispatched.
    assert (await client.post(f"/api/v1/admin/transfers/{tid}/dispatch", headers=h)).status_code == 409


async def test_transfer_cannot_cancel_in_transit(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "TR-NOCANCEL-A")
    src = await _default_loc(client, h)
    dst = await _location(client, h, "TR Hub NoCancel")
    await client.post(
        "/api/v1/admin/stock/batches",
        json={"product_id": pid, "batch_no": "LOT-NC", "qty": 10, "location_id": src, "expiry_date": "2041-01-01"},
        headers=idem(h),
    )
    tid = (await client.post(
        "/api/v1/admin/transfers",
        json={"to_location_id": dst, "items": [{"product_id": pid, "qty": 4}]},
        headers=h,
    )).json()["id"]
    await client.post(f"/api/v1/admin/transfers/{tid}/dispatch", headers=h)
    assert (await client.post(f"/api/v1/admin/transfers/{tid}/cancel", headers=h)).status_code == 409


async def test_transfer_list_get_and_admin_gate(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _product(client, h, "TR-GATE-A")
    dst = await _location(client, h, "TR Hub Gate")
    tid = (await client.post(
        "/api/v1/admin/transfers",
        json={"to_location_id": dst, "items": [{"product_id": pid, "qty": 1}]},
        headers=h,
    )).json()["id"]
    assert (await client.get(f"/api/v1/admin/transfers/{tid}", headers=h)).json()["id"] == tid
    listed = await client.get("/api/v1/admin/transfers?status_filter=draft", headers=h)
    assert any(t["id"] == tid for t in listed.json())
    assert (await client.get("/api/v1/admin/transfers/999999", headers=h)).status_code == 404

    cust = await login(client, "9876543210")
    hc = auth(cust)
    assert (await client.get("/api/v1/admin/transfers", headers=hc)).status_code == 403
    assert (await client.post("/api/v1/admin/transfers", json={"to_location_id": dst, "items": [{"product_id": pid, "qty": 1}]}, headers=hc)).status_code == 403
