from conftest import auth, login


async def _customer_id(client, token) -> int:
    # WhatsApp is opt-in: turning it on records the whatsapp_optin consent.
    await client.patch("/api/v1/profile", json={"notify_whatsapp": True}, headers=auth(token))
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
    # WhatsApp opt-in (order updates) does NOT opt the user in to marketing.
    wa = next(c for c in created if c["channel"] == "whatsapp")
    assert wa["skip_reason"] == "no_marketing_optin"


async def test_consent_then_flush_delivers(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000000946")  # T6: own throwaway user, not shared 9876543210
    cid = await _customer_id(client, cust)
    await client.post(
        "/api/v1/consents",
        json={"purpose": "whatsapp_optin", "action": "granted"},
        headers=auth(cust),
    )
    await client.post(
        "/api/v1/consents",
        json={"purpose": "marketing_whatsapp", "action": "granted"},
        headers=auth(cust),
    )
    created = (await client.post(
        "/api/v1/admin/notifications/test",
        json={"user_id": cid, "event_type": "marketing.promo", "data": {"message": "hi"}},
        headers=auth(admin),
    )).json()["created"]
    # The whatsapp channel must be QUEUED (consent granted), not skipped.
    wa = next(c for c in created if c["channel"] == "whatsapp")
    assert wa["status"] == "queued", wa
    # Drain until THIS row is processed: the outbox is FIFO and other tests may have left
    # a backlog larger than one flush batch.
    import sqlalchemy as sa

    from app.core.db import engine

    status = None
    for _ in range(10):
        await client.post("/api/v1/admin/notifications/flush?limit=500", headers=auth(admin))
        async with engine.connect() as conn:
            status = (await conn.execute(
                sa.text("SELECT status FROM notifications WHERE id=:i"), {"i": wa["id"]}
            )).scalar()
        if status != "queued":
            break
    assert status == "sent", "the consented marketing.promo must be sent"


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
    # A TRANSACTIONAL event that TARGETS sms (payment.failed → whatsapp/sms/push) must
    # still queue on the SMS channel after the marketing-sms opt-out.
    r = await client.post(
        "/api/v1/admin/notifications/test",
        json={"user_id": cid, "event_type": "payment.failed",
              "data": {"amount": 200, "balance": 200}},
        headers=auth(admin),
    )
    created = r.json()["created"]
    sms = next(c for c in created if c["channel"] == "sms")
    assert sms["status"] == "queued", sms  # NOT skipped channel_off
    # And the profile's SMS channel toggle is untouched by the marketing opt-out.
    prof = (await client.get("/api/v1/profile", headers=auth(cust))).json()
    assert prof["notify_sms"] is True
    assert prof["marketing_opt_in"] is False
