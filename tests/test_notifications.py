from conftest import auth, login


async def _customer_id(client, token) -> int:
    return (await client.get("/api/v1/profile", headers=auth(token))).json()["user_id"]


async def test_transactional_queued(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    cid = await _customer_id(client, cust)
    r = await client.post(
        "/api/v1/admin/notifications/test",
        json={
            "user_id": cid,
            "event_type": "payment.success",
            "data": {"amount": 200, "balance": 200},
        },
        headers=auth(admin),
    )
    created = r.json()["created"]
    wa = next(c for c in created if c["channel"] == "whatsapp")
    assert wa["status"] == "queued"


async def test_marketing_skipped_without_consent(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    cid = await _customer_id(client, cust)
    r = await client.post(
        "/api/v1/admin/notifications/test",
        json={"user_id": cid, "event_type": "marketing.promo", "data": {"message": "hi"}},
        headers=auth(admin),
    )
    created = r.json()["created"]
    assert all(c["status"] == "skipped" for c in created)
    assert created[0]["skip_reason"] == "no_marketing_optin"


async def test_consent_then_flush_delivers(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    cid = await _customer_id(client, cust)
    await client.post(
        "/api/v1/consents",
        json={"purpose": "whatsapp_optin", "action": "granted"},
        headers=auth(cust),
    )
    await client.post(
        "/api/v1/admin/notifications/test",
        json={"user_id": cid, "event_type": "marketing.promo", "data": {"message": "hi"}},
        headers=auth(admin),
    )
    flush = await client.post("/api/v1/admin/notifications/flush", headers=auth(admin))
    assert flush.json()["sent"] >= 1
    queued = await client.get(
        "/api/v1/admin/notifications?status_filter=queued", headers=auth(admin)
    )
    assert queued.json() == []
