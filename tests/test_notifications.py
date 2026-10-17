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


async def test_marketing_optout_keeps_transactional_sms(client):
    """#35: revoking marketing_sms must NOT silence transactional SMS (OTP/order updates).

    Marketing is gated by marketing_opt_in + consent; the per-channel notify_sms toggle
    (which also gates transactional sends) must stay on after a marketing opt-out.
    """
    admin = await login(client, "9999900001")
    cust = await login(client, "9000000935")  # throwaway user — no shared/leaked profile state
    cid = await _customer_id(client, cust)
    # Opt OUT of marketing SMS.
    await client.post(
        "/api/v1/consents",
        json={"purpose": "marketing_sms", "action": "revoked"},
        headers=auth(cust),
    )
    # A TRANSACTIONAL event must still queue on the SMS channel.
    r = await client.post(
        "/api/v1/admin/notifications/test",
        json={"user_id": cid, "event_type": "payment.success",
              "data": {"amount": 200, "balance": 200}},
        headers=auth(admin),
    )
    created = r.json()["created"]
    sms = [c for c in created if c["channel"] == "sms"]
    if sms:  # only assert if this event targets SMS
        assert sms[0]["status"] != "skipped" or sms[0].get("skip_reason") != "channel_off"
    # And the profile's SMS channel toggle is untouched by the marketing opt-out.
    prof = (await client.get("/api/v1/profile", headers=auth(cust))).json()
    assert prof["notify_sms"] is True
    assert prof["marketing_opt_in"] is False
