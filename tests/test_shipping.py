from conftest import auth, login


async def _order_id(client, cust_h, qty=2) -> int:
    prods = (await client.get("/api/v1/products", headers=cust_h)).json()
    pid = next(p["id"] for p in prods if p["sku"] == "MILK-1L")
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": qty}], "address": "12 MG Road, Jaipur"},
        headers=cust_h,
    )
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    # A non-COD (prepaid) order must be PAID before it can ship — drive it through the
    # dev payment gateway so the realistic prepaid flow is exercised.
    create = await client.post(
        "/api/v1/payments/create", json={"purpose": "order_payment", "order_id": oid},
        headers=cust_h,
    )
    goid = create.json()["gateway_order_id"]
    await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=cust_h,
    )
    return oid


async def _create_shipment(client, admin_h, oid, **over):
    body = {"order_id": oid, "pincode": "302001", "name": "Aarav", "phone": "9876543210", "weight_kg": 1.0}
    body.update(over)
    return await client.post("/api/v1/admin/shipments", json=body, headers=admin_h)


async def test_serviceability_devstub(client):
    cust = await login(client, "9876543210")
    r = await client.get("/api/v1/shipping/serviceability?pincode=560001&weight_kg=1", headers=auth(cust))
    assert r.status_code == 200
    body = r.json()
    assert body["serviceable"] is True and body["courier"] == "DEV-Courier"


async def test_create_shipment_and_track(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    ch = auth(cust)
    oid = await _order_id(client, ch)

    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 200, r.text
    ship = r.json()
    assert ship["status"] == "created"
    assert ship["awb"].startswith("DEVAWB")
    assert ship["shipment_no"].startswith("SHP")
    assert any(e["status"] == "created" for e in ship["events"])
    sid, awb = ship["id"], ship["awb"]

    # Customer sees their own shipment.
    mine = await client.get(f"/api/v1/shipments/{sid}", headers=ch)
    assert mine.status_code == 200 and mine.json()["id"] == sid

    # Webhook advances status.
    w1 = await client.post("/api/v1/shipping/webhook", json={"awb": awb, "current_status": "In Transit"})
    assert w1.status_code == 200 and w1.json()["status"] == "ok"
    assert (await client.get(f"/api/v1/shipments/{sid}", headers=ah)).json()["status"] == "in_transit"

    # Deliver, then a duplicate delivered webhook is idempotent.
    await client.post("/api/v1/shipping/webhook", json={"awb": awb, "current_status": "Delivered"})
    dup = await client.post("/api/v1/shipping/webhook", json={"awb": awb, "current_status": "Delivered"})
    assert dup.json()["status"] == "duplicate"
    final = (await client.get(f"/api/v1/shipments/{sid}", headers=ah)).json()
    assert final["status"] == "delivered"


async def test_webhook_unknown_awb(client):
    r = await client.post("/api/v1/shipping/webhook", json={"awb": "NOSUCHAWB", "current_status": "Delivered"})
    assert r.status_code == 200 and r.json()["status"] == "unknown_awb"


async def test_shipment_idor_404(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    oid = await _order_id(client, auth(cust))
    ship = (await _create_shipment(client, auth(admin), oid)).json()
    # A different customer must not see it (404, not 403).
    other = await login(client, "9000000123")
    r = await client.get(f"/api/v1/shipments/{ship['id']}", headers=auth(other))
    assert r.status_code == 404


async def test_create_shipment_requires_admin(client):
    cust = await login(client, "9876543210")
    oid = await _order_id(client, auth(cust))
    r = await _create_shipment(client, auth(cust), oid)  # customer token
    assert r.status_code == 403


async def test_cancel_shipment(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    oid = await _order_id(client, auth(cust))
    ship = (await _create_shipment(client, ah, oid)).json()
    c = await client.post(f"/api/v1/admin/shipments/{ship['id']}/cancel", headers=ah)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    # Cannot cancel a terminal shipment again.
    again = await client.post(f"/api/v1/admin/shipments/{ship['id']}/cancel", headers=ah)
    assert again.status_code == 409


async def _unpaid_order_id(client, cust_h, qty=1) -> int:
    prods = (await client.get("/api/v1/products", headers=cust_h)).json()
    pid = next(p["id"] for p in prods if p["sku"] == "MILK-1L")
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": qty}], "address": "7 Station Rd, Jaipur"},
        headers=cust_h,
    )
    return r.json()["id"]


async def test_prepaid_unpaid_order_cannot_ship_but_cod_can(client):
    """#79: a non-COD shipment requires a paid order; a COD shipment may ship unpaid."""
    ah = auth(await login(client, "9999900001"))
    ch = auth(await login(client, "9876543210"))

    oid = await _unpaid_order_id(client, ch)
    blocked = await _create_shipment(client, ah, oid)  # non-COD, order still pending
    assert blocked.status_code == 409

    oid2 = await _unpaid_order_id(client, ch)
    cod_ok = await _create_shipment(client, ah, oid2, cod=True)
    assert cod_ok.status_code == 200, cod_ok.text


async def test_create_shipment_unknown_order_404(client):
    admin = await login(client, "9999900001")
    r = await _create_shipment(client, auth(admin), 999999)
    assert r.status_code == 404
