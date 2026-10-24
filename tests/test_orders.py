from conftest import auth, login


async def test_create_order_prices_lines(client):
    cust = await login(client, "9876543210")
    r = await client.post(
        "/api/v1/orders", json={"items": [{"product_id": 1, "qty": 2}]}, headers=auth(cust)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 54.0  # 2 x RETAIL 27.00
    assert body["payment_status"] == "pending"


async def test_empty_order_rejected(client):
    cust = await login(client, "9876543210")
    r = await client.post("/api/v1/orders", json={"items": []}, headers=auth(cust))
    assert r.status_code == 422


async def test_order_idor(client):
    owner = await login(client, "9876543210")
    attacker = await login(client, "9191919191")
    o = await client.post(
        "/api/v1/orders", json={"items": [{"product_id": 1, "qty": 1}]}, headers=auth(owner)
    )
    oid = o.json()["id"]
    r = await client.get(f"/api/v1/orders/{oid}", headers=auth(attacker))
    assert r.status_code == 404


async def test_order_payment_flow(client):
    cust = await login(client, "9876543210")
    order = (
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": 1, "qty": 2}]}, headers=auth(cust)
        )
    ).json()
    oid = order["id"]

    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "order_payment", "order_id": oid, "amount": 1},  # amount is ignored
        headers=auth(cust),
    )
    assert create.status_code == 200
    assert create.json()["amount"] == 54.0  # server-authoritative, not the client's 1
    goid = create.json()["gateway_order_id"]

    verify = await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=auth(cust),
    )
    assert verify.status_code == 200

    paid = await client.get(f"/api/v1/orders/{oid}", headers=auth(cust))
    assert paid.json()["payment_status"] == "paid"

    # Paying an already-paid order is rejected.
    again = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "order_payment", "order_id": oid},
        headers=auth(cust),
    )
    assert again.status_code == 409


async def test_no_double_open_order_payment(client):
    cust = await login(client, "9876543210")
    order = (
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": 1, "qty": 1}]}, headers=auth(cust)
        )
    ).json()
    first = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "order_payment", "order_id": order["id"]},
        headers=auth(cust),
    )
    assert first.status_code == 200
    # M3: a second create RESUMES the open checkout (same payment + gateway order) — it
    # never opens a second gateway order (no double charge) and never wedges the order.
    second = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "order_payment", "order_id": order["id"]},
        headers=auth(cust),
    )
    assert second.status_code == 200
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert second.json()["gateway_order_id"] == first.json()["gateway_order_id"]
    # Once paid, creating another payment is refused.
    await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": first.json()["gateway_order_id"], "gateway_payment_id": "p",
              "signature": "dev"},
        headers=auth(cust),
    )
    third = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "order_payment", "order_id": order["id"]},
        headers=auth(cust),
    )
    assert third.status_code == 409


async def test_interstate_d2c_order_invoice_uses_igst(client):
    """#M12: a D2C order that captures an interstate delivery state must invoice IGST
    (not CGST/SGST). Seller state is Rajasthan; delivering to Maharashtra is interstate."""
    cust = await login(client, "9000000712")  # fresh retail user, no prior home shop
    ch = auth(cust)
    # Ghee is a 12% GST item so tax is non-zero and the split is observable.
    prods = (await client.get("/api/v1/products", headers=ch)).json()
    ghee = next(p["id"] for p in prods if p["sku"] == "GHEE-1KG")
    oid = (
        await client.post(
            "/api/v1/orders",
            json={
                "items": [{"product_id": ghee, "qty": 1}],
                "address": "1 Linking Rd, Mumbai",
                "state": "Maharashtra",
                "pincode": "400050",
            },
            headers=ch,
        )
    ).json()["id"]
    # Pay it (invoice is issued only for a paid order).
    create = await client.post(
        "/api/v1/payments/create", json={"purpose": "order_payment", "order_id": oid}, headers=ch
    )
    goid = create.json()["gateway_order_id"]
    await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=ch,
    )
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=ch)).json()
    assert inv["is_interstate"] is True
    assert inv["igst"] > 0 and inv["cgst"] == 0.0 and inv["sgst"] == 0.0


async def test_duplicate_product_lines_merged(client):
    """P3 #7: two request lines for the same product collapse into one order line (summed
    qty), so qty-break pricing and the invoice aren't distorted by duplicate lines."""
    cust = await login(client, "9000000771")
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": 1, "qty": 2}, {"product_id": 1, "qty": 3}]},
        headers=auth(cust),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    lines = [li for li in body["items"] if li["product_id"] == 1]
    assert len(lines) == 1 and lines[0]["qty"] == 5.0
