from conftest import auth, login


async def _wallet(client, token) -> float:
    d = (await client.get("/api/v1/billing/me", headers=auth(token))).json()
    return float(d["wallet_balance"])


async def test_webhook_finalizes_unverified_payment(client):
    cust = await login(client, "9876543210")
    before = await _wallet(client, cust)
    create = await client.post(
        "/api/v1/payments/create",
        json={"purpose": "wallet_topup", "amount": 120},
        headers=auth(cust),
    )
    goid = create.json()["gateway_order_id"]

    # Client never called /verify; the gateway webhook captures it.
    event = {
        "id": "evt_cap_1",
        "event": "payment.captured",
        "order_id": goid,
        "payment_id": "pay_hook",
    }
    r = await client.post("/api/v1/payments/webhook", json=event)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert round(await _wallet(client, cust) - before, 2) == 120.0

    # Duplicate delivery of the same event is a no-op (wallet not credited twice).
    dup = await client.post("/api/v1/payments/webhook", json=event)
    assert dup.json()["status"] == "duplicate"
    assert round(await _wallet(client, cust) - before, 2) == 120.0


async def test_webhook_unknown_event_ok(client):
    r = await client.post(
        "/api/v1/payments/webhook", json={"id": "evt_x", "event": "subscription.updated"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
