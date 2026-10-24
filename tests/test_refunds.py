from conftest import auth, login


async def _wallet(client, token) -> float:
    await client.get("/api/v1/loyalty", headers=auth(token))  # ensure the account exists
    d = (await client.get("/api/v1/billing/me", headers=auth(token))).json()
    return float(d["wallet_balance"])


async def _topup(client, token, amount) -> int:
    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "wallet_topup", "amount": amount},
        headers=auth(token),
    )
    goid = create.json()["gateway_order_id"]
    await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "pay_x", "signature": "dev"},
        headers=auth(token),
    )
    return create.json()["payment_id"]


async def test_partial_then_full_refund(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    pid = await _topup(client, cust, 200)
    start = await _wallet(client, cust)  # includes the 200 just added

    r1 = await client.post(f"/api/v1/payments/{pid}/refund", json={"amount": 50}, headers=auth(admin))
    assert r1.status_code == 200
    assert round(start - await _wallet(client, cust), 2) == 50.0  # reversal pulls 50 out

    # Over-refunding the remaining 150 is rejected.
    mid = await _wallet(client, cust)
    over = await client.post(
        f"/api/v1/payments/{pid}/refund", json={"amount": 200}, headers=auth(admin)
    )
    assert over.status_code == 422
    assert await _wallet(client, cust) == mid  # rejected refund moved nothing

    # Full remaining refund (no amount = remaining 150).
    r2 = await client.post(f"/api/v1/payments/{pid}/refund", json={}, headers=auth(admin))
    assert r2.status_code == 200
    assert r2.json()["amount"] == 150.0
    assert round(start - await _wallet(client, cust), 2) == 200.0  # whole top-up reversed
    # Nothing left to refund.
    assert (await client.post(f"/api/v1/payments/{pid}/refund", json={}, headers=auth(admin))).status_code == 422


async def test_refund_requires_admin(client):
    import sqlalchemy as sa

    from app.core.db import engine

    cust = await login(client, "9876543210")
    pid = await _topup(client, cust, 100)
    before = await _wallet(client, cust)
    r = await client.post(f"/api/v1/payments/{pid}/refund", json={"amount": 10}, headers=auth(cust))
    assert r.status_code == 403
    assert await _wallet(client, cust) == before
    async with engine.connect() as conn:
        n = (await conn.execute(sa.text("SELECT count(*) FROM refunds WHERE payment_id=:p"), {"p": pid})).scalar()
    assert n == 0


async def test_refund_only_successful(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    # An initiated (unverified) payment cannot be refunded.
    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "wallet_topup", "amount": 30},
        headers=auth(cust),
    )
    pid = create.json()["payment_id"]
    r = await client.post(f"/api/v1/payments/{pid}/refund", json={}, headers=auth(admin))
    assert r.status_code == 409


async def test_order_payment_refund_marks_order(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    order = (
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": 1, "qty": 2}]}, headers=auth(cust)
        )
    ).json()
    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "order_payment", "order_id": order["id"]},
        headers=auth(cust),
    )
    goid = create.json()["gateway_order_id"]
    pid = create.json()["payment_id"]
    await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=auth(cust),
    )
    before = await _wallet(client, cust)
    r = await client.post(f"/api/v1/payments/{pid}/refund", json={}, headers=auth(admin))
    assert r.status_code == 200
    assert r.json()["amount"] == 54.0
    # Order-payment refund goes BACK TO SOURCE (card/UPI) via the gateway — the wallet
    # is NOT credited (crediting it too was the double-refund bug).
    assert round(await _wallet(client, cust) - before, 2) == 0.0
    o = (await client.get(f"/api/v1/orders/{order['id']}", headers=auth(cust))).json()
    assert o["payment_status"] == "refunded"
