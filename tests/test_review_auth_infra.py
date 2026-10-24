"""Regression tests for the review's auth (A*), DPDP (P*), notification (N*) and
infra (I*) fixes, see scratchpad/MASTER_FIXLIST.md."""
import asyncio
import time

import pytest
import sqlalchemy as sa
from conftest import auth, erase_confirm, login
from starlette.requests import Request

from app.core.db import SessionLocal, engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _admin(client):
    return auth(await login(client, "9999900001"))


async def _tokens(client, phone):
    otp = (await client.post("/api/v1/auth/otp/request", json={"phone": phone})).json()["dev_otp"]
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": phone, "otp": otp})
    return r


def _req(headers: dict, peer="10.0.0.5") -> Request:
    return Request({
        "type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 1234), "method": "GET", "path": "/",
    })


# ---------- A2: client IP is not spoofable through X-Forwarded-For ----------
def test_client_ip_counts_trusted_hops_from_the_right(monkeypatch):
    from app.core import clientip
    from app.core.config import settings

    spoofed = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}  # client-sent 6.6.6.6 + proxy-appended
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    assert clientip.client_ip(_req(spoofed)) == "203.0.113.9"
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)  # no proxy: header ignored
    assert clientip.client_ip(_req(spoofed)) == "10.0.0.5"


# ---------- A3: staff phone canonicalized like OTP login ----------
async def test_staff_created_with_prefix_can_log_in(client):
    ah = await _admin(client)
    r = await client.post("/api/v1/admin/users", json={"phone": "919811122233", "role": "delivery"}, headers=ah)
    assert r.status_code == 201 and r.json()["phone"] == "9811122233"
    me = await client.get("/api/v1/auth/me", headers=auth(await login(client, "9811122233")))
    assert me.json()["role"] == "delivery"
    assert (await client.post("/api/v1/admin/users", json={"phone": "449811122233", "role": "delivery"},
                              headers=ah)).status_code == 422


# ---------- A4/A5/P1/P6: erasure is final and complete ----------
async def test_erasure_revokes_cancels_tombstones_and_is_final(client):
    ah = await _admin(client)
    tok = await _tokens(client, "9000000950")
    access, refresh = tok.json()["access_token"], tok.json()["refresh_token"]
    sid = (await client.post("/api/v1/subscriptions", json={"home_address": "42 Secret Lane", "home_state": "RJ"},
                             headers=auth(access))).json()["id"]
    uid = (await _q("SELECT id FROM users WHERE phone='9000000950'"))[0][0]
    await _q("INSERT INTO notifications (user_id, event_type, channel, category, status, to_addr, body) "
             "VALUES (:u, 'x', 'sms', 'transactional', 'queued', '9000000950', 'hi')", u=uid)
    assert (await client.post("/api/v1/account/erase", json=await erase_confirm(client, "9000000950"),
                              headers=auth(access))).status_code == 200
    shop = await _q("SELECT s.name, s.address, s.status FROM shops s JOIN subscriptions sub ON sub.shop_id=s.id "
                    "WHERE sub.id=:i", i=sid)
    assert shop == [("erased", None, "inactive")]
    assert (await _q("SELECT status FROM subscriptions WHERE id=:i", i=sid))[0][0] == "cancelled"
    assert (await _q("SELECT status, skip_reason FROM notifications WHERE user_id=:u AND event_type='x'",
                     u=uid)) == [("skipped", "user_erased")]
    # Refresh tokens are revoked; the account can't be resurrected by an admin.
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})).status_code == 401
    assert (await client.patch(f"/api/v1/admin/users/{uid}", json={"status": "active"}, headers=ah)).status_code == 409


async def test_suspended_user_gets_403_not_tokens(client):
    ah = await _admin(client)
    await login(client, "9000000951")
    uid = (await _q("SELECT id FROM users WHERE phone='9000000951'"))[0][0]
    await client.patch(f"/api/v1/admin/users/{uid}", json={"status": "suspended"}, headers=ah)
    before = (await _q("SELECT count(*) FROM auth_sessions WHERE user_id=:u", u=uid))[0][0]
    r = await _tokens(client, "9000000951")
    assert r.status_code == 403
    assert (await _q("SELECT count(*) FROM auth_sessions WHERE user_id=:u", u=uid))[0][0] == before


# ---------- A6: refresh reuse kills outstanding access tokens ----------
async def test_refresh_reuse_invalidates_access_tokens(client):
    first = (await _tokens(client, "9000000952")).json()
    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    new_access = rotated.json()["access_token"]
    assert (await client.get("/api/v1/auth/me", headers=auth(new_access))).status_code == 200
    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert reuse.status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=auth(new_access))).status_code == 401


# ---------- A7: foreign numbers are not mapped onto Indian ones ----------
def test_phone_normalization_prefixes():
    from app.core.phone import is_indian_mobile, normalize_phone

    for v in ("+91 98765 43210", "0091-9876543210", "09876543210", "919876543210", "9876543210"):
        assert normalize_phone(v) == "9876543210"
    assert not is_indian_mobile(normalize_phone("447911123456"))
    assert not is_indian_mobile(normalize_phone("+1 415 555 98765"))


# ---------- A8: email format + ownership verification ----------
async def test_email_must_be_valid_and_verified(client):
    h = auth(await login(client, "9000000953"))
    assert (await client.patch("/api/v1/profile", json={"email": "not-an-email"}, headers=h)).status_code == 422
    p = await client.patch("/api/v1/profile", json={"email": "a953@example.com"}, headers=h)
    assert p.status_code == 200 and p.json()["email_verified"] is False
    assert (await client.post("/api/v1/account/email/verify", json={"token": "x" * 20}, headers=h)).status_code == 400
    tok = (await client.post("/api/v1/account/email/verify-request", headers=h)).json()["dev_token"]
    v = await client.post("/api/v1/account/email/verify", json={"token": tok}, headers=h)
    assert v.status_code == 200 and v.json()["email_verified"] is True
    # Changing the address drops verification; the old token no longer verifies.
    await client.patch("/api/v1/profile", json={"email": "b953@example.com"}, headers=h)
    assert (await client.get("/api/v1/profile", headers=h)).json()["email_verified"] is False
    assert (await client.post("/api/v1/account/email/verify", json={"token": tok}, headers=h)).status_code == 400


async def test_erasure_keeps_newsletter_of_unverified_email(client):
    victim = "victim954@example.com"
    await _q("INSERT INTO newsletter_subscribers (email, status) VALUES (:e, 'subscribed')", e=victim)
    h = auth(await login(client, "9000000954"))
    await client.patch("/api/v1/profile", json={"email": victim}, headers=h)  # squat, never verified
    assert (await client.post("/api/v1/account/erase", json=await erase_confirm(client, "9000000954"),
                              headers=h)).status_code == 200
    assert (await _q("SELECT status FROM newsletter_subscribers WHERE email=:e", e=victim)) == [("subscribed",)]


# ---------- A9: an OTP can be consumed once, even concurrently ----------
async def test_concurrent_otp_verify_only_one_wins(client):
    otp = (await client.post("/api/v1/auth/otp/request", json={"phone": "9000000955"})).json()["dev_otp"]
    rs = await asyncio.gather(*[
        client.post("/api/v1/auth/otp/verify", json={"phone": "9000000955", "otp": otp}) for _ in range(4)
    ])
    assert sorted(r.status_code for r in rs) == [200, 401, 401, 401]


# ---------- P2/P3/N4: gating re-checked at send ----------
async def _user(phone):
    from app.models.user import User

    async with SessionLocal() as db:
        return await db.scalar(sa.select(User).where(User.phone == phone))


async def test_marketing_consent_rechecked_at_delivery(client):
    from app.services import notifications as notify

    h = auth(await login(client, "9000000956"))
    await client.post("/api/v1/consents", json={"purpose": "whatsapp_optin", "action": "granted"}, headers=h)
    await client.post("/api/v1/consents", json={"purpose": "marketing_whatsapp", "action": "granted"}, headers=h)
    user = await _user("9000000956")
    async with SessionLocal() as db:
        rows = await notify.emit_event(db, "review.request", user, {"order_id": 1}, dedupe_key="rr956")
        await db.commit()
        wa = [r for r in rows if r.channel == "whatsapp"]
        assert wa and wa[0].status == "queued"
        wid = wa[0].id
    # Withdraw ONLY the consent (append to the ledger; the channel toggle stays on), so the
    # send-time consent re-check is what must stop it.
    await _q("INSERT INTO consents (user_id, purpose, action, source) VALUES (:u, 'marketing_whatsapp', 'withdrawn', 'app')",
             u=user.id)
    async with SessionLocal() as db:
        from app.models.notification import Notification

        n = await db.get(Notification, wid)
        assert await notify.deliver_notification(db, n) is False
    assert (await _q("SELECT status, skip_reason FROM notifications WHERE id=:i", i=wid)) == [
        ("skipped", "consent_withdrawn")]


async def test_marketing_push_requires_its_own_consent(client):
    from app.services import notifications as notify

    h = auth(await login(client, "9000000957"))
    # Marketing on (via an SMS marketing consent) and push on — but no marketing_push consent.
    await client.post("/api/v1/consents", json={"purpose": "marketing_sms", "action": "granted"}, headers=h)
    await client.patch("/api/v1/profile", json={"notify_push": True}, headers=h)
    user = await _user("9000000957")
    async with SessionLocal() as db:
        rows = await notify.emit_event(db, "review.request", user, {"order_id": 1}, dedupe_key="rr957")
        await db.commit()
    push = [r for r in rows if r.channel == "push"]
    assert all(r.status == "skipped" and r.skip_reason == "no_consent" for r in push)


async def test_deactivated_push_token_not_used_at_delivery(client):
    from app.services import notifications as notify

    h = auth(await login(client, "9000000958"))
    token = "t" * 80 + "958"
    await client.post("/api/v1/account/device-tokens", json={"token": token}, headers=h)
    user = await _user("9000000958")
    async with SessionLocal() as db:
        rows = await notify.emit_event(db, "payment.success", user, {"amount": 1, "balance": 1}, dedupe_key="ps958")
        await db.commit()
        push = next(r for r in rows if r.channel == "push")
        assert push.status == "queued"
    await client.post("/api/v1/account/device-tokens/unregister", json={"token": token}, headers=h)
    async with SessionLocal() as db:
        from app.models.notification import Notification

        n = await db.get(Notification, push.id)
        await notify.deliver_notification(db, n)
    assert (await _q("SELECT status, skip_reason FROM notifications WHERE id=:i", i=push.id)) == [
        ("skipped", "no_address")]


# ---------- P4: unconfigured channels don't fake success in prod / don't log PII ----------
async def test_stub_sender_fails_in_prod_and_masks_pii(monkeypatch, caplog):
    from app.core.config import settings
    from app.services.notifications import senders

    ok, _, _ = await senders.send_sms("9876543210", "secret order body", None)
    assert ok and "9876543210" not in caplog.text and "secret order body" not in caplog.text
    monkeypatch.setattr(settings, "ENV", "prod")
    ok, _, err = await senders.send_sms("9876543210", "x", None)
    assert ok is False and "not configured" in err


# ---------- P5: export covers every PII-bearing table ----------
async def test_export_is_complete(client):
    h = auth(await login(client, "9000000959"))
    await client.post("/api/v1/subscriptions", json={"home_address": "7 Export Rd"}, headers=h)
    exp = (await client.get("/api/v1/account/export", headers=h)).json()
    for key in ("shops", "subscriptions", "notifications", "auth_sessions", "wallet", "payments",
                "delivery_orders", "shipments", "returns", "cart_items", "wishlist", "points_ledger",
                "ticket_messages", "newsletter", "driver_locations", "accounts"):
        assert key in exp, key
    assert any(s["address"] == "7 Export Rd" for s in exp["shops"])
    assert exp["auth_sessions"] and "refresh_hash" not in exp["auth_sessions"][0]
    assert "password_hash" not in exp["user"] and "token_version" not in exp["user"]
    # Every column of the user/profile rows is present (generic serializer).
    from app.models.profile import UserProfile

    assert {c.key for c in UserProfile.__table__.columns} <= set(exp["profile"])


# ---------- P7: newsletter confirm tokens expire ----------
async def test_newsletter_confirm_token_expires(client):
    expired = f"abc123def456.{int(time.time()) - 10}"
    await _q("INSERT INTO newsletter_subscribers (email, status, confirm_token) VALUES ('x960@example.com', 'pending', :t)",
             t=expired)
    assert (await client.post("/api/v1/newsletter/confirm", json={"token": expired})).status_code == 404
    r = await client.post("/api/v1/newsletter/subscribe", json={"email": "y960@example.com"})
    tok = r.json()["confirm_token"]
    assert tok.rsplit(".", 1)[1].isdigit()
    assert (await client.post("/api/v1/newsletter/confirm", json={"token": tok})).status_code == 200


# ---------- N1: a failing emit never poisons the caller's transaction ----------
async def test_emit_failure_does_not_roll_back_caller(monkeypatch, client):
    from app.services import notifications as notify
    from app.services.notifications import service as nsvc

    await login(client, "9000000961")
    user = await _user("9000000961")

    async def boom(db, *a, **k):
        await db.execute(sa.text("SELECT 1/0"))  # puts the session in a failed state

    monkeypatch.setattr(nsvc, "_emit_event_inner", boom)
    async with SessionLocal() as db:
        await db.execute(sa.text("UPDATE users SET full_name='kept961' WHERE id=:i"), {"i": user.id})
        assert await notify.emit_event(db, "payment.success", user, {"amount": 1, "balance": 1}) == []
        await db.commit()  # used to raise PendingRollbackError → caller's write lost
    assert (await _q("SELECT full_name FROM users WHERE id=:i", i=user.id))[0][0] == "kept961"


# ---------- N3: concurrent device registration ----------
async def test_concurrent_device_register_same_token(client):
    h = auth(await login(client, "9000000962"))
    token = "z" * 90 + "962"
    rs = await asyncio.gather(*[
        client.post("/api/v1/account/device-tokens", json={"token": token}, headers=h) for _ in range(4)
    ])
    assert all(r.status_code == 200 for r in rs), [r.text for r in rs]
    assert (await _q("SELECT count(*) FROM device_tokens WHERE token=:t", t=token))[0][0] == 1


# ---------- I1: chunked bodies are capped too ----------
async def test_chunked_body_over_cap_rejected(client):
    async def gen():
        chunk = b"x" * (1024 * 1024)
        for _ in range(9):
            yield chunk

    r = await client.post("/api/v1/payments/create", content=gen(),
                          headers={"Idempotency-Key": "chunk1", "Content-Type": "application/json"})
    assert r.status_code == 413


# ---------- I2: null on a NOT NULL CMS field is a 422 ----------
async def test_cms_patch_null_is_422(client):
    ah = await _admin(client)
    art = (await client.post("/api/v1/admin/content/articles", json={
        "slug": "null-test-963", "title": "T", "body_html": "<p>x</p>"}, headers=ah)).json()
    r = await client.patch(f"/api/v1/admin/content/articles/{art['id']}", json={"title": None}, headers=ah)
    assert r.status_code == 422


# ---------- I3: concurrent churn runs don't collide ----------
async def test_concurrent_churn_scoring(client):
    from app.core.redis import redis_client
    from app.services.ml import churn

    await redis_client.delete(churn._LOCK_KEY)

    async def run():
        async with SessionLocal() as db:
            return await churn.score_all(db)

    a, b = await asyncio.gather(run(), run())
    assert a["scored"] + b["scored"] >= 1  # one ran (the other may have yielded to the lock)
    dup = await _q("SELECT account_id FROM churn_scores GROUP BY account_id HAVING count(*) > 1")
    assert dup == []


# ---------- I4: SSRF guard ----------
def test_ssrf_guard_blocks_cgnat_and_narrows_dev_bypass():
    from app.services import admin_tools

    assert admin_tools._resolve_public("http://100.100.100.200/latest/meta-data") is None
    assert admin_tools._resolve_public("http://169.254.169.254/") is None
    assert admin_tools._resolve_public("http://10.1.2.3/") is None
    assert admin_tools._is_dev_url("http://localhost/hook")
    assert not admin_tools._is_dev_url("https://developer.partner.com/hook")


# ---------- I7: readiness never leaks exception text ----------
async def test_readyz_hides_error_details(client, monkeypatch):
    from app.api.v1 import health

    async def down():
        raise RuntimeError("redis://admin:hunter2@10.0.0.9 refused")

    monkeypatch.setattr(health.redis_client, "ping", down)
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["redis"] == "error"
    assert "hunter2" not in r.text


# ---------- I8: paid AI fails closed without a quota store ----------
async def test_ai_quota_fails_closed_when_live(monkeypatch):
    from fastapi import HTTPException

    from app.services.ai import client as ai_client
    from app.services.ai import quota

    async def broken(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(quota.redis_client, "incr", broken)
    await quota.check_and_increment(1)  # dev stub: allowed
    monkeypatch.setattr(ai_client, "ai_enabled", lambda: True)
    with pytest.raises(HTTPException) as e:
        await quota.check_and_increment(1)
    assert e.value.status_code == 503


def test_ai_retries_only_transient_errors():
    from app.services.ai.client import _is_transient

    class E(Exception):
        def __init__(self, code):
            self.status_code = code

    assert _is_transient(TimeoutError()) and _is_transient(E(429)) and _is_transient(E(503))
    assert not _is_transient(E(400)) and not _is_transient(E(401)) and not _is_transient(ValueError())


# ---------- I9: auth responses (tokens) are never stored for replay ----------
async def test_idempotency_never_stores_auth_responses(client):
    otp = (await client.post("/api/v1/auth/otp/request", json={"phone": "9000000964"})).json()["dev_otp"]
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": "9000000964", "otp": otp},
                          headers={"Idempotency-Key": "auth-964"})
    assert r.status_code == 200
    assert (await _q("SELECT count(*) FROM idempotency_keys WHERE key LIKE '%auth-964'"))[0][0] == 0


# ---------- I10: the rate-limit window key always gets a TTL ----------
async def test_rate_limit_key_has_ttl():
    from app.core import ratelimit
    from app.core.redis import redis_client

    assert await ratelimit.allow("203.0.113.77", 5)
    keys = [k async for k in redis_client.scan_iter("rl:203.0.113.77:*")]
    ttls = [await redis_client.ttl(k) for k in keys]
    assert keys and all(t > 0 for t in ttls)


# ---------- M11: concurrent withdrawals can't exceed the balance ----------
async def test_concurrent_withdrawals_cannot_overdraw(client):
    h = auth(await login(client, "9000000965"))
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 100},
                             headers=h)).json()
    await client.post("/api/v1/payments/verify", json={"gateway_order_id": pay["gateway_order_id"],
                                                       "gateway_payment_id": "p", "signature": "dev"}, headers=h)
    rs = await asyncio.gather(*[
        client.post("/api/v1/billing/withdrawals", json={"amount": 80}, headers=h) for _ in range(3)
    ])
    assert sorted(r.status_code for r in rs) == [200, 422, 422]
