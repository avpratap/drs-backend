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
