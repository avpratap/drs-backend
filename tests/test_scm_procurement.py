from uuid import uuid4

from conftest import auth, login


def _idem(h: dict) -> dict:
    """Auth headers + a fresh Idempotency-Key (required on the GRN receive endpoint)."""
    return {**h, "Idempotency-Key": uuid4().hex}


async def _product(client, h, sku) -> int:
    return (await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)).json()["id"]


async def _stock(client, h, pid) -> float:
    rows = (await client.get("/api/v1/admin/stock", headers=h)).json()
    return next((s["qty_on_hand"] for s in rows if s["product_id"] == pid), 0.0)


async def _supplier(client, h, name="Acme Dairy Supplies") -> int:
    r = await client.post(
        "/api/v1/admin/suppliers",
        json={"name": name, "gstin": "08ABCDE1234F1Z5", "state": "Rajasthan"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_create_po_computes_totals(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    sup = await _supplier(client, h, "PO-TOTALS-SUP")
    p1 = await _product(client, h, "PO-TOT-A")
    p2 = await _product(client, h, "PO-TOT-B")
    r = await client.post(
        "/api/v1/admin/purchase-orders",
        json={
            "supplier_id": sup,
            "items": [
                {"product_id": p1, "qty": 10, "unit_cost": 12.5},
                {"product_id": p2, "qty": 3, "unit_cost": 100},
            ],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    po = r.json()
    assert po["status"] == "draft"
    assert po["po_no"].startswith("PO")
    assert po["subtotal"] == 425.0  # 10*12.5 + 3*100
    assert po["total"] == 425.0
    assert len(po["items"]) == 2
    line_a = next(i for i in po["items"] if i["product_id"] == p1)
    assert line_a["line_total"] == 125.0 and line_a["qty_received"] == 0.0


async def test_create_po_unknown_supplier_404(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    p1 = await _product(client, h, "PO-BADSUP-A")
    r = await client.post(
        "/api/v1/admin/purchase-orders",
        json={"supplier_id": 999999, "items": [{"product_id": p1, "qty": 1, "unit_cost": 5}]},
        headers=h,
    )
    assert r.status_code == 404


async def test_po_full_receipt_flow(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    sup = await _supplier(client, h, "PO-FULL-SUP")
    pid = await _product(client, h, "PO-FULL-A")
    start = await _stock(client, h, pid)

    po = (await client.post(
        "/api/v1/admin/purchase-orders",
        json={"supplier_id": sup, "items": [{"product_id": pid, "qty": 20, "unit_cost": 8}]},
        headers=h,
    )).json()
    po_id = po["id"]

    # Cannot receive a draft PO.
    early = await client.post(
        f"/api/v1/admin/purchase-orders/{po_id}/receive",
        json={"lines": [{"product_id": pid, "qty_received": 5, "batch_no": "X"}]},
        headers=_idem(h),
    )
    assert early.status_code == 409

    placed = await client.post(f"/api/v1/admin/purchase-orders/{po_id}/place", headers=h)
    assert placed.status_code == 200 and placed.json()["status"] == "placed"

    rec = await client.post(
        f"/api/v1/admin/purchase-orders/{po_id}/receive",
        json={"lines": [{"product_id": pid, "qty_received": 20, "batch_no": "GRN-FULL", "expiry_date": "2040-01-01"}]},
        headers=_idem(h),
    )
    assert rec.status_code == 200, rec.text
    body = rec.json()
    assert body["status"] == "received"
    assert next(i for i in body["items"] if i["product_id"] == pid)["qty_received"] == 20.0
    # GRN created a stock batch and incremented on-hand.
    assert await _stock(client, h, pid) == start + 20.0
    batches = (await client.get(f"/api/v1/admin/stock/batches?product_id={pid}&active_only=false", headers=h)).json()
    assert any(b["batch_no"] == "GRN-FULL" and b["qty"] == 20.0 for b in batches)


async def test_po_partial_then_complete(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    sup = await _supplier(client, h, "PO-PART-SUP")
    pid = await _product(client, h, "PO-PART-A")

    po_id = (await client.post(
        "/api/v1/admin/purchase-orders",
        json={"supplier_id": sup, "items": [{"product_id": pid, "qty": 30, "unit_cost": 4}]},
        headers=h,
    )).json()["id"]
    await client.post(f"/api/v1/admin/purchase-orders/{po_id}/place", headers=h)

    r1 = await client.post(
        f"/api/v1/admin/purchase-orders/{po_id}/receive",
        json={"lines": [{"product_id": pid, "qty_received": 12, "batch_no": "GRN-P1"}]},
        headers=_idem(h),
    )
    assert r1.status_code == 200 and r1.json()["status"] == "partially_received"

    # Over-receipt relative to the remaining 18 is rejected.
    over = await client.post(
        f"/api/v1/admin/purchase-orders/{po_id}/receive",
        json={"lines": [{"product_id": pid, "qty_received": 25, "batch_no": "GRN-OVER"}]},
        headers=_idem(h),
    )
    assert over.status_code == 422

    r2 = await client.post(
        f"/api/v1/admin/purchase-orders/{po_id}/receive",
        json={"lines": [{"product_id": pid, "qty_received": 18, "batch_no": "GRN-P2"}]},
        headers=_idem(h),
    )
    assert r2.status_code == 200 and r2.json()["status"] == "received"


async def test_po_receive_product_not_on_po_422(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    sup = await _supplier(client, h, "PO-WRONG-SUP")
    pid = await _product(client, h, "PO-WRONG-A")
    other = await _product(client, h, "PO-WRONG-B")
    po_id = (await client.post(
        "/api/v1/admin/purchase-orders",
        json={"supplier_id": sup, "items": [{"product_id": pid, "qty": 5, "unit_cost": 1}]},
        headers=h,
    )).json()["id"]
    await client.post(f"/api/v1/admin/purchase-orders/{po_id}/place", headers=h)
    r = await client.post(
        f"/api/v1/admin/purchase-orders/{po_id}/receive",
        json={"lines": [{"product_id": other, "qty_received": 1, "batch_no": "NOPE"}]},
        headers=_idem(h),
    )
    assert r.status_code == 422


async def test_po_cancel(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    sup = await _supplier(client, h, "PO-CANCEL-SUP")
    pid = await _product(client, h, "PO-CANCEL-A")
    po_id = (await client.post(
        "/api/v1/admin/purchase-orders",
        json={"supplier_id": sup, "items": [{"product_id": pid, "qty": 5, "unit_cost": 1}]},
        headers=h,
    )).json()["id"]
    c = await client.post(f"/api/v1/admin/purchase-orders/{po_id}/cancel", headers=h)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    # A cancelled PO cannot be placed or received.
    assert (await client.post(f"/api/v1/admin/purchase-orders/{po_id}/place", headers=h)).status_code == 409


async def test_po_list_and_get(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    sup = await _supplier(client, h, "PO-LIST-SUP")
    pid = await _product(client, h, "PO-LIST-A")
    po_id = (await client.post(
        "/api/v1/admin/purchase-orders",
        json={"supplier_id": sup, "items": [{"product_id": pid, "qty": 2, "unit_cost": 3}]},
        headers=h,
    )).json()["id"]
    got = await client.get(f"/api/v1/admin/purchase-orders/{po_id}", headers=h)
    assert got.status_code == 200 and got.json()["id"] == po_id
    listed = await client.get("/api/v1/admin/purchase-orders?status_filter=draft", headers=h)
    assert listed.status_code == 200 and any(p["id"] == po_id for p in listed.json())
    assert (await client.get("/api/v1/admin/purchase-orders/999999", headers=h)).status_code == 404


async def test_procurement_requires_admin(client):
    cust = await login(client, "9876543210")
    hc = auth(cust)
    assert (await client.get("/api/v1/admin/suppliers", headers=hc)).status_code == 403
    assert (await client.get("/api/v1/admin/purchase-orders", headers=hc)).status_code == 403
    assert (await client.post("/api/v1/admin/suppliers", json={"name": "x"}, headers=hc)).status_code == 403


async def test_create_po_unknown_product_422(client):
    """#31: an unknown product_id on a PO line returns 422, not a FK 500."""
    h = auth(await login(client, "9999900001"))
    sup = await _supplier(client, h, "Ghost Supplier")
    r = await client.post(
        "/api/v1/admin/purchase-orders",
        json={"supplier_id": sup, "items": [{"product_id": 99999999, "qty": 1, "unit_cost": 10}]},
        headers=h,
    )
    assert r.status_code == 422
