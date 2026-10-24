"""Tier-4 #35: device tokens, admin resend, new event templates, retry backoff."""

from conftest import auth, login


async def _customer_id(client, h) -> int:
    # WhatsApp is opt-in: turning it on records the whatsapp_optin consent.
    await client.patch("/api/v1/profile", json={"notify_whatsapp": True}, headers=h)
    return (await client.get("/api/v1/auth/me", headers=h)).json()["id"]


async def test_device_token_register_idempotent(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    tok = "fcmtok" + "d" * 80  # realistic FCM token length (min_length=64)
    r1 = await client.post("/api/v1/account/device-tokens", json={"token": tok, "platform": "android"}, headers=h)
    assert r1.status_code == 200 and r1.json()["active"] is True
    # Re-registering the same token is idempotent (stays one active row).
    r2 = await client.post("/api/v1/account/device-tokens", json={"token": tok, "platform": "ios"}, headers=h)
    assert r2.status_code == 200 and r2.json()["platform"] == "ios"
    d = await client.delete(f"/api/v1/account/device-tokens/{tok}", headers=h)
    assert d.status_code == 204


async def test_new_event_templates_emit(client):
    """The new transactional events render and queue a row (registry wired)."""
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    cid = await _customer_id(client, auth(cust))
    for event, data in [
        ("payment.refunded", {"amount": 100, "balance": 150}),
        ("subscription.created", {}),
        ("shipment.delivered", {"shipment_no": "SHP000001"}),
    ]:
        r = await client.post(
            "/api/v1/admin/notifications/test",
            json={"user_id": cid, "event_type": event, "data": data, "dedupe_key": f"{event}:test"},
            headers=ah,
        )
        assert r.status_code == 200
        created = r.json()["created"]
        assert created, f"{event} produced no rows"
        # whatsapp channel should be queued (default toggle on, transactional).
        assert any(c["channel"] == "whatsapp" and c["status"] == "queued" for c in created)


async def test_admin_resend_requeues(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    cid = await _customer_id(client, auth(cust))
    emit = await client.post(
        "/api/v1/admin/notifications/test",
        json={"user_id": cid, "event_type": "payment.success", "data": {"amount": 10, "balance": 10}},
        headers=ah,
    )
    nid = next(c["id"] for c in emit.json()["created"] if c["channel"] == "whatsapp")
    await client.post("/api/v1/admin/notifications/flush", headers=ah)  # -> sent
    resend = await client.post(f"/api/v1/admin/notifications/{nid}/resend", headers=ah)
    assert resend.status_code == 200 and resend.json()["status"] == "queued"


async def test_subscription_lifecycle_emits(client):
    """Pausing then resuming a subscription emits lifecycle notifications to its owner.
    Uses the subscriber's OWN new subscription — pausing the seeded one leaked into every
    cutoff test whenever an assert failed before the resume."""
    sub = await login(client, "9999900002")
    sh = auth(sub)
    sid_me = (await client.get("/api/v1/auth/me", headers=sh)).json()["id"]
    shop = (await client.post("/api/v1/shops", json={"name": "Lifecycle shop"}, headers=sh)).json()["id"]
    sub_id = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=sh)).json()["id"]
    admin = auth(await login(client, "9999900001"))

    assert (await client.post(f"/api/v1/subscriptions/{sub_id}/pause", headers=sh)).status_code == 200
    q = f"/api/v1/admin/notifications?user_id={sid_me}&limit=200"
    assert any(n["event_type"] == "subscription.paused" for n in (await client.get(q, headers=admin)).json())
    assert (await client.post(f"/api/v1/subscriptions/{sub_id}/resume", headers=sh)).status_code == 200
    assert any(n["event_type"] == "subscription.resumed" for n in (await client.get(q, headers=admin)).json())


async def test_retry_backoff_defers_next_attempt(client, monkeypatch):
    from app.core.db import SessionLocal
    from app.models.notification import Notification
    from app.services.notifications import senders as sender_mod
    from app.services.notifications import service as svc

    async def _fail(to_addr, body, payload):
        return False, None, "boom"

    monkeypatch.setitem(sender_mod.SENDERS, "whatsapp", _fail)
    async with SessionLocal() as db:
        import sqlalchemy as sa

        uid = (await db.execute(sa.text("SELECT id FROM users LIMIT 1"))).scalar()
        from app.models.user import User
        from app.services import profile as profile_svc

        (await profile_svc.get_or_create_profile(db, await db.get(User, uid))).notify_whatsapp = True
        notif = Notification(
            user_id=uid, event_type="payment.success", channel="whatsapp",
            category="transactional", to_addr="9876543210", body="hi", status="queued",
        )
        db.add(notif)
        await db.flush()
        ok = await svc.deliver_notification(db, notif)
        assert ok is False
        assert notif.status == "queued"  # not yet failed (attempts < MAX)
        assert notif.next_attempt_at is not None  # backoff scheduled
        assert notif.last_attempt_at is not None
        await db.rollback()
