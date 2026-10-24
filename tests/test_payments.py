from conftest import auth, login


async def _wallet_balance(client, token) -> float:
    # Ensure the (lazily created) account exists; then read the real field — the old
    # .get(..., 0) fallback hid a missing account / renamed field as a 0 balance.
    await client.get("/api/v1/loyalty", headers=auth(token))
    r = await client.get("/api/v1/billing/me", headers=auth(token))
    assert r.status_code == 200, r.text
    return float(r.json()["wallet_balance"])


async def test_double_verify_credits_once(client):
    token = await login(client, "9876543210")
    before = await _wallet_balance(client, token)
    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "wallet_topup", "amount": 200},
        headers=auth(token),
    )
    goid = create.json()["gateway_order_id"]
    verify_body = {"gateway_order_id": goid, "gateway_payment_id": "pay_x", "signature": "dev"}

    r1 = await client.post("/api/v1/payments/verify", json=verify_body, headers=auth(token))
    assert r1.status_code == 200
    r2 = await client.post("/api/v1/payments/verify", json=verify_body, headers=auth(token))
    assert r2.status_code == 200  # idempotent replay, not a second credit

    after = await _wallet_balance(client, token)
    assert round(after - before, 2) == 200.0


async def test_payment_idor_blocked(client):
    owner = await login(client, "9876543210")
    attacker = await login(client, "9191919191")
    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "wallet_topup", "amount": 50},
        headers=auth(owner),
    )
    goid = create.json()["gateway_order_id"]
    owner_before = await _wallet_balance(client, owner)
    r = await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=auth(attacker),
    )
    assert r.status_code == 404
    # The attacker's attempt changed nothing: still open, no credit, and the real owner
    # can still complete it.
    assert await _wallet_balance(client, owner) == owner_before
    ok = await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=auth(owner),
    )
    assert ok.status_code == 200 and ok.json()["status"] == "success"
    assert round(await _wallet_balance(client, owner) - owner_before, 2) == 50.0


async def test_negative_amount_rejected(client):
    token = await login(client, "9876543210")
    r = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "wallet_topup", "amount": -1},
        headers=auth(token),
    )
    assert r.status_code == 422


async def test_topup_requires_amount(client):
    token = await login(client, "9876543210")
    r = await client.post(
        "/api/v1/payments/create", json={"purpose": "wallet_topup"}, headers=auth(token)
    )
    assert r.status_code == 422
