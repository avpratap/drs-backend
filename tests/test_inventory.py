from conftest import auth, idem, login


async def _fresh_product(client, h, sku) -> int:
    return (
        await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)
    ).json()["id"]


async def test_stock_receipt_wastage_and_movements(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    # Dedicated product so other tests' stock activity can't skew the absolute math.
    pid = await _fresh_product(client, h, "STK-WASTE-1")

    r1 = await client.post(
        "/api/v1/admin/stock/adjust",
        json={"product_id": pid, "change": 100, "reason": "receipt"},
        headers=idem(h),
    )
    assert r1.status_code == 200 and r1.json()["qty_on_hand"] == 100.0
    r2 = await client.post(
        "/api/v1/admin/stock/wastage",
        json={"product_id": pid, "qty": 15, "note": "spoilage"},
        headers=idem(h),
    )
    assert r2.json()["qty_on_hand"] == 85.0

    lst = await client.get("/api/v1/admin/stock", headers=h)
    assert any(s["product_id"] == pid and s["qty_on_hand"] == 85.0 for s in lst.json())

    moves = (await client.get(f"/api/v1/admin/stock/{pid}/movements", headers=h)).json()
    reasons = {m["reason"] for m in moves}
    assert "receipt" in reasons and "wastage" in reasons


async def test_stock_adjust_validation(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    # zero change rejected
    assert (await client.post("/api/v1/admin/stock/adjust", json={"product_id": 1, "change": 0, "reason": "receipt"}, headers=idem(h))).status_code == 422
    # bad reason rejected
    assert (await client.post("/api/v1/admin/stock/adjust", json={"product_id": 1, "change": 5, "reason": "magic"}, headers=idem(h))).status_code == 422
    # unknown product
    assert (await client.post("/api/v1/admin/stock/adjust", json={"product_id": 999999, "change": 5, "reason": "receipt"}, headers=idem(h))).status_code == 404
    # negative wastage qty rejected
    assert (await client.post("/api/v1/admin/stock/wastage", json={"product_id": 1, "qty": -3}, headers=idem(h))).status_code == 422


async def test_stock_requires_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/stock", headers=auth(cust))).status_code == 403
