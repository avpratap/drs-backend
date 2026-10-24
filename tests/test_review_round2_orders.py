"""Regression tests for the second review round: orders / notifications / auth / DPDP
(R2-O*). IDs refer to scratchpad/MASTER_FIXLIST.md."""
import sqlalchemy as sa
from conftest import auth, erase_confirm, ik, login

from app.core.db import SessionLocal, engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _milk() -> int:
    return (await _q("SELECT id FROM products WHERE sku='MILK-1L'"))[0][0]


async def _user(phone):
    from app.models.user import User

    async with SessionLocal() as db:
        return await db.scalar(sa.select(User).where(User.phone == phone))


# ---------- R2-O1: COD orders are paid on delivery; their coupon isn't blocked ----------
async def test_cod_order_paid_on_delivery_and_coupon_reusable(client):
    ah = auth(await login(client, "9999900001"))
    ch = auth(await login(client, "9000001101"))
    await client.post("/api/v1/admin/coupons", json={
        "code": "COD1101", "discount_type": "flat", "value": 2}, headers=ah)
    o = (await client.post("/api/v1/orders", json={"items": [{"product_id": await _milk(), "qty": 2}],
                                                   "coupon_code": "COD1101"}, headers=ch)).json()
    s = await client.post("/api/v1/admin/shipments", json={"order_id": o["id"], "pincode": "302001", "cod": True},
                          headers=ah)
    assert s.status_code == 200, s.text
    # Shipped COD order: a real purchase, not a hoarded reservation.
    again = await client.post("/api/v1/orders", json={"items": [{"product_id": await _milk(), "qty": 2}],
                                                      "coupon_code": "COD1101"}, headers=ch)
    assert again.status_code == 200, again.text
    await client.post("/api/v1/shipping/webhook", json={"awb": s.json()["awb"], "current_status": "Delivered"})
    order = (await client.get(f"/api/v1/orders/{o['id']}", headers=ch)).json()
    assert order["payment_status"] == "paid"
    assert (await client.post(f"/api/v1/orders/{o['id']}/invoice", headers=ch)).status_code == 200


# ---------- R2-O2: unimplemented transactional SMS is skipped in prod, not retried ----------
async def test_sms_skipped_when_provider_unavailable_in_prod(client, monkeypatch):
    from app.core.config import settings
    from app.services import notifications as notify

    await login(client, "9000001102")
    user = await _user("9000001102")
    monkeypatch.setattr(settings, "ENV", "prod")
    async with SessionLocal() as db:
        rows = await notify.emit_event(db, "wallet.low", user, {"balance": 1},
                                       dedupe_key="sms1102")
        await db.rollback()
    sms = [r for r in rows if r.channel == "sms"]
    assert sms and all(r.status == "skipped" and r.skip_reason == "provider_unavailable" for r in sms)


# ---------- R2-O3: unverified email claims don't lock the owner out; throttled sends ----------
async def test_email_unique_only_when_verified_and_throttled(client):
    a = auth(await login(client, "9000001103"))
    b = auth(await login(client, "9000001104"))
    # A squats the address (unverified) — B can still set it.
    assert (await client.patch("/api/v1/profile", json={"email": "Owner1103@Example.com"},
                               headers=a)).status_code == 200
    assert (await client.patch("/api/v1/profile", json={"email": "owner1103@example.com"},
                               headers=b)).status_code == 200
    tok = (await client.post("/api/v1/account/email/verify-request", headers=b)).json()["dev_token"]
    # A second request inside the cooldown is refused (no inbox flooding).
    assert (await client.post("/api/v1/account/email/verify-request", headers=b)).status_code == 429
    assert (await client.post("/api/v1/account/email/verify", json={"token": tok}, headers=b)).status_code == 200
    # The address was just mailed: A's request is throttled per RECIPIENT, not per user.
    assert (await client.post("/api/v1/account/email/verify-request", headers=a)).status_code == 429
    from app.core.redis import redis_client

    await redis_client.flushdb()
    # Now verified by B: A can't verify the same address.
    tok_a = (await client.post("/api/v1/account/email/verify-request", headers=a)).json()["dev_token"]
    assert (await client.post("/api/v1/account/email/verify", json={"token": tok_a},
                              headers=a)).status_code == 409


async def test_email_with_pipe_can_be_verified(client):
    h = auth(await login(client, "9000001105"))
    assert (await client.patch("/api/v1/profile", json={"email": "a|b1105@example.com"}, headers=h)).status_code == 200
    tok = (await client.post("/api/v1/account/email/verify-request", headers=h)).json()["dev_token"]
    r = await client.post("/api/v1/account/email/verify", json={"token": tok}, headers=h)
    assert r.status_code == 200 and r.json()["email_verified"] is True


async def test_newsletter_subscribe_throttled_and_non_enumerating(client):
    r1 = await client.post("/api/v1/newsletter/subscribe", json={"email": "n1106@example.com"})
    token = r1.json()["confirm_token"]
    await client.post("/api/v1/newsletter/confirm", json={"token": token})
    r2 = await client.post("/api/v1/newsletter/subscribe", json={"email": "n1106@example.com"})
    assert r2.status_code == 200 and r2.json()["status"] == "pending"  # doesn't reveal 'subscribed'
    assert r2.json().get("confirm_token") is None  # throttled: no new token rotated in


# ---------- R2-O4: WhatsApp opt-in isn't marketing consent; bell hides gated marketing ----------
async def test_whatsapp_optin_does_not_grant_marketing(client):
    from app.services import notifications as notify

    h = auth(await login(client, "9000001107"))
    await client.post("/api/v1/consents", json={"purpose": "whatsapp_optin", "action": "granted"}, headers=h)
    prof = (await client.get("/api/v1/profile", headers=h)).json()
    assert prof["notify_whatsapp"] is True and prof["marketing_opt_in"] is False
    user = await _user("9000001107")
    async with SessionLocal() as db:
        rows = await notify.emit_event(db, "marketing.promo", user, {"message": "x"}, dedupe_key="mk1107")
        await db.commit()
    assert all(r.status == "skipped" for r in rows)
    bell = (await client.get("/api/v1/notifications", headers=h)).json()
    assert not [n for n in bell if n["event_type"] == "marketing.promo"]
    # marketing_opt_in can't be set without a consent record.
    await client.patch("/api/v1/profile", json={"marketing_opt_in": True}, headers=h)
    assert (await client.get("/api/v1/profile", headers=h)).json()["marketing_opt_in"] is False


async def test_whatsapp_toggle_is_recorded_as_consent(client):
    h = auth(await login(client, "9000001108"))
    assert (await client.get("/api/v1/profile", headers=h)).json()["notify_whatsapp"] is False  # opt-in
    await client.patch("/api/v1/profile", json={"notify_whatsapp": True}, headers=h)
    user = await _user("9000001108")
    rows = await _q("SELECT action FROM consents WHERE user_id=:u AND purpose='whatsapp_optin'", u=user.id)
    assert rows == [("granted",)]


# ---------- R2-O5: address default can't be unset / set on an archived row ----------
async def test_address_default_rules(client):
    from app.schemas.profile import AddressUpdate
    from app.services import profile as svc

    h = auth(await login(client, "9000001109"))
    body = {"line1": "1 Road", "city": "Jaipur", "state": "Rajasthan", "pincode": "302001"}
    a1 = (await client.post("/api/v1/addresses", json=body, headers=h)).json()
    assert a1["is_default"] is True
    r = await client.patch(f"/api/v1/addresses/{a1['id']}", json={"is_default": False}, headers=h)
    assert r.status_code == 422
    # Race: the row is archived after the caller's check but before the lock.
    from app.models.profile import Address

    async with SessionLocal() as db:
        addr = await db.get(Address, a1["id"])
        await _q("UPDATE addresses SET status='archived', is_default=false WHERE id=:i", i=a1["id"])
        from fastapi import HTTPException

        try:
            await svc.update_address(db, addr, AddressUpdate(is_default=True))
            raised = False
        except HTTPException as exc:
            raised = exc.status_code == 404
        await db.rollback()
    assert raised


# ---------- R2-O6: erasure cancels open deliveries, waits for pending withdrawals ----------
async def test_erasure_blocked_by_pending_withdrawal(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001110"))
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 100},
                             headers=h)).json()
    await client.post("/api/v1/payments/verify", json={"gateway_order_id": pay["gateway_order_id"],
                                                       "gateway_payment_id": "p", "signature": "dev"}, headers=h)
    wd = (await client.post("/api/v1/billing/withdrawals", json={"amount": 50}, headers=h)).json()
    assert (await client.post("/api/v1/account/erase", json=await erase_confirm(client, "9000001110"),
                              headers=h)).status_code == 409
    await client.post(f"/api/v1/admin/withdrawals/{wd['id']}/reject", headers=ah)
    assert (await client.post("/api/v1/account/erase", json=await erase_confirm(client, "9000001110"),
                              headers=h)).status_code == 200


# ---------- R2-O7: uploads are private references with metadata stripped ----------
async def test_upload_is_private_ref_with_exif_stripped(client):
    import os

    from app.core.config import settings

    h = auth(await login(client, "9000001111"))
    exif = b"\xff\xe1\x00\x11Exif\x00\x00GPSDATA!!"
    jpeg = b"\xff\xd8" + b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + exif + b"\xff\xda\x00\x02DATA\xff\xd9"
    r = await client.post("/api/v1/uploads", files={"file": ("p.jpg", jpeg, "image/jpeg")}, headers=h)
    assert r.status_code == 200, r.text
    ref = r.json()["url"]
    assert ref.startswith("media:private/uploads/") and "exp=" in r.json()["preview_url"]
    stored = open(os.path.join(settings.STORAGE_DIR, ref[len("media:"):]), "rb").read()
    assert b"GPSDATA" not in stored and stored.startswith(b"\xff\xd8\xff\xe0")
    # Another user can't attach it.
    other = auth(await login(client, "9000001112"))
    t = (await client.post("/api/v1/support/tickets", json={"subject": "s", "body": "b"}, headers=other)).json()
    bad = await client.post(f"/api/v1/support/tickets/{t['id']}/messages",
                            json={"body": "x", "attachment_url": ref}, headers=other)
    assert bad.status_code == 422


# ---------- R2-O8: small input rules ----------
def test_phone_with_country_code_and_trunk_zero():
    from app.core.phone import normalize_phone

    assert normalize_phone("+91 09876543210") == "9876543210"
    assert normalize_phone("0091 9876543210") == "9876543210"


async def test_whole_units_enforced_in_cart(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001113"))
    pid = (await client.post("/api/v1/products", json={"sku": "PCS-1113", "name": "egg", "uom": "Pcs"},
                             headers=ah)).json()["id"]
    assert (await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 1.5}, headers=h)).status_code == 422
    assert (await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 2}, headers=h)).status_code == 200


async def test_review_votes_rules(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001114"))
    other = auth(await login(client, "9000001115"))
    milk = await _milk()
    rid = (await client.post(f"/api/v1/products/{milk}/ratings", json={"stars": 5}, headers=h)).json()["id"]
    url = f"/api/v1/products/{milk}/ratings/{rid}/helpful"
    # Not purchased -> pending: not votable.
    assert (await client.post(url, headers=other)).status_code == 404
    await client.post(f"/api/v1/admin/ratings/{rid}/moderate", json={"status": "approved"}, headers=ah)
    await _q("UPDATE ratings SET status='approved' WHERE id=:i", i=rid)
    assert (await client.post(url, headers=h)).status_code == 409  # own review
    assert (await client.post(url, headers=other)).status_code == 200


async def test_mandate_cancel_and_cascade_on_method_removal(client):
    h = auth(await login(client, "9000001116"))
    pm = (await client.post("/api/v1/payment-methods", json={"method_type": "upi", "display": "x@upi"},
                            headers=h)).json()
    m = (await client.post("/api/v1/mandates", json={"payment_method_id": pm["id"], "max_amount": 500},
                           headers=h)).json()
    assert (await client.post("/api/v1/mandates", json={"max_amount": 5, "valid_to": "2020-01-01"},
                              headers=h)).status_code == 422
    assert (await client.delete(f"/api/v1/payment-methods/{pm['id']}", headers=h)).status_code == 204
    assert (await _q("SELECT status FROM payment_mandates WHERE id=:i", i=m["id"]))[0][0] == "cancelled"
    m2 = (await client.post("/api/v1/mandates", json={"max_amount": 500}, headers=h)).json()
    r = await client.post(f"/api/v1/mandates/{m2['id']}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


async def test_admin_can_set_account_type(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001117"))
    await client.get("/api/v1/loyalty", headers=h)  # ensure the account exists
    acct = (await client.get("/api/v1/billing/me", headers=h)).json()["account_id"]
    r = await client.patch(f"/api/v1/admin/accounts/{acct}/type",
                           json={"account_type": "b2b", "billing_mode": "postpaid_credit"}, headers=ah)
    assert r.status_code == 200 and r.json()["account_type"] == "b2b"
    assert ik  # (fixture helper import kept for parity with other review tests)


# ---------- R3-2: erasing your own account needs a fresh OTP ----------
async def test_self_erasure_requires_fresh_otp(client):
    h = auth(await login(client, "9000001120"))
    assert (await client.post("/api/v1/account/erase", headers=h)).status_code == 422  # no OTP
    assert (await client.post("/api/v1/account/erase", json={"otp": "000000"}, headers=h)).status_code == 403
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 200  # still there
    ok = await client.post("/api/v1/account/erase", json=await erase_confirm(client, "9000001120"), headers=h)
    assert ok.status_code == 200


# ---------- R3-3: payment-method tokens come from the gateway, never the client ----------
async def test_payment_method_token_only_from_verified_payment(client):
    h = auth(await login(client, "9000001121"))
    other = auth(await login(client, "9000001122"))
    forged = await client.post("/api/v1/payment-methods", json={
        "method_type": "card", "gateway_token": "token_someone_else"}, headers=h)
    assert forged.status_code == 422
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 10},
                             headers=h)).json()
    await client.post("/api/v1/payments/verify", json={
        "gateway_order_id": pay["gateway_order_id"], "gateway_payment_id": "pay_r3_1121", "signature": "dev"},
        headers=h)
    # Another user can't save my payment's instrument.
    assert (await client.post("/api/v1/payment-methods/from-payment", json={"payment_id": pay["payment_id"]},
                              headers=other)).status_code == 404
    r = await client.post("/api/v1/payment-methods/from-payment", json={"payment_id": pay["payment_id"]},
                          headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["method_type"] == "upi" and r.json()["display"] == "dev@upi"
    again = await client.post("/api/v1/payment-methods/from-payment", json={"payment_id": pay["payment_id"]},
                              headers=h)
    assert again.json()["id"] == r.json()["id"]  # idempotent
    token = (await _q("SELECT gateway_token FROM payment_methods WHERE id=:i", i=r.json()["id"]))[0][0]
    assert token.startswith("token_dev_")
