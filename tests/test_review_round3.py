"""Regression tests for the third (focused) review round (R3-*); see
scratchpad/MASTER_FIXLIST.md."""
import datetime as dt
import os

import sqlalchemy as sa
from conftest import auth, erase_confirm, ik, login

from app.core.db import SessionLocal, engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _milk() -> int:
    return (await _q("SELECT id FROM products WHERE sku='MILK-1L'"))[0][0]


async def _paid(client, h, items):
    o = (await client.post("/api/v1/orders", json={"items": items}, headers=h)).json()
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "order_payment", "order_id": o["id"]},
                             headers=h)).json()
    await client.post("/api/v1/payments/verify", json={
        "gateway_order_id": pay["gateway_order_id"], "gateway_payment_id": "p", "signature": "dev"}, headers=h)
    return o["id"], pay["payment_id"]


# ---------- R3-4: email notifications only to a verified inbox ----------
async def test_unverified_email_gets_no_notification(client):
    from app.models.user import User
    from app.services import notifications as notify

    h = auth(await login(client, "9000001301"))
    await client.patch("/api/v1/profile", json={"email": "victim1301@example.com"}, headers=h)
    async with SessionLocal() as db:
        user = await db.scalar(sa.select(User).where(User.phone == "9000001301"))
        rows = await notify.emit_event(db, "payment.success", user, {"amount": 1, "balance": 1},
                                       dedupe_key="em1301")
        await db.rollback()
    assert all(not (r.channel == "email" and r.status == "queued") for r in rows)


# ---------- R3-5: address state must be a real state (GST place of supply) ----------
async def test_address_state_validated(client):
    h = auth(await login(client, "9000001302"))
    body = {"line1": "1 Rd", "city": "X", "state": "Mars", "pincode": "302001"}
    assert (await client.post("/api/v1/addresses", json=body, headers=h)).status_code == 422
    body["state"] = "rj"
    r = await client.post("/api/v1/addresses", json=body, headers=h)
    assert r.status_code in (200, 201) and r.json()["state"] == "Rajasthan"


# ---------- R3-6: erasure deletes the stored uploads ----------
async def test_erasure_deletes_uploaded_files(client):
    import io

    from PIL import Image

    from app.core.config import settings

    h = auth(await login(client, "9000001303"))
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "PNG")
    ref = (await client.post("/api/v1/uploads", files={"file": ("a.png", buf.getvalue(), "image/png")},
                             headers=h)).json()["url"]
    path = os.path.join(settings.STORAGE_DIR, ref[len("media:"):])
    assert os.path.isfile(path)
    assert (await client.post("/api/v1/account/erase", json=await erase_confirm(client, "9000001303"),
                              headers=h)).status_code == 200
    assert not os.path.exists(path)


# ---------- R3-7: credit notes follow SETTLED refunds; RMA-only refunds are caught up ----------
async def test_rma_before_invoice_is_credited(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001304"))
    milk = await _milk()
    oid, _ = await _paid(client, h, [{"product_id": milk, "qty": 4}])
    await _q("UPDATE orders SET status='delivered' WHERE id=:o", o=oid)
    rid = (await client.post("/api/v1/returns", json={"order_id": oid, "items": [{"product_id": milk, "qty": 1}]},
                             headers=h)).json()["id"]
    await client.post(f"/api/v1/admin/returns/{rid}/approve", headers=ah)
    assert (await client.post(f"/api/v1/admin/returns/{rid}/refund", json={"amount": 27},
                              headers=ah)).status_code == 200
    order = (await client.get(f"/api/v1/orders/{oid}", headers=h)).json()
    assert order["payment_status"] == "partially_refunded"
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=h)).json()
    [(total,)] = await _q("SELECT total FROM invoices WHERE original_invoice_id=:i", i=inv["id"])
    assert float(total) == -27.0


async def test_pending_refund_not_in_credit_note(client, monkeypatch):
    from app.services import payments as pay_service

    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001305"))
    oid, pay_id = await _paid(client, h, [{"product_id": await _milk(), "qty": 4}])
    assert (await client.post(f"/api/v1/payments/{pay_id}/refund", json={"amount": 30},
                              headers=ik(ah))).status_code == 200

    async def timeout(*a, **k):
        raise TimeoutError("x")

    monkeypatch.setattr(pay_service, "create_gateway_refund", timeout)
    assert (await client.post(f"/api/v1/payments/{pay_id}/refund", json={"amount": 20},
                              headers=ik(ah))).status_code == 502  # pending, outcome unknown
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=h)).json()
    [(total,)] = await _q("SELECT total FROM invoices WHERE original_invoice_id=:i", i=inv["id"])
    assert float(total) == -30.0  # only the processed refund


# ---------- R3-8: spending uses cash first — credit can't be laundered into cash ----------
async def test_spent_cash_then_gift_credit_not_withdrawable(client):
    from app.services import billing

    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001306"))
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 1000},
                             headers=h)).json()
    await client.post("/api/v1/payments/verify", json={
        "gateway_order_id": pay["gateway_order_id"], "gateway_payment_id": "p", "signature": "dev"}, headers=h)
    acct = (await client.get("/api/v1/billing/me", headers=h)).json()["account_id"]
    async with SessionLocal() as db:
        await billing.wallet_adjust(db, acct, -1000, "debit_order")
        await db.commit()
    gc = (await client.post("/api/v1/admin/gift-cards", json={"amount": 500, "issued_to_account_id": acct},
                            headers=ik(ah))).json()
    await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": gc["code"], "amount": 500}, headers=h)
    assert (await client.post("/api/v1/billing/withdrawals", json={"amount": 100},
                              headers=h)).status_code == 422
    # Nor can it go back to the card as a "top-up refund".
    assert (await client.post(f"/api/v1/payments/{pay['payment_id']}/refund", json={"amount": 100},
                              headers=ik(ah))).status_code == 422


# ---------- R3-9: no referral reward for a ₹0 order ----------
async def test_referral_not_rewarded_for_free_order(client):
    ah = auth(await login(client, "9999900001"))
    referrer = auth(await login(client, "9000001307"))
    referee = auth(await login(client, "9000001308"))
    code = (await client.get("/api/v1/loyalty/referral-code", headers=referrer)).json()["code"]
    await client.post("/api/v1/loyalty/referral/redeem", json={"code": code}, headers=referee)
    await client.post("/api/v1/admin/coupons", json={"code": "FREE1308", "discount_type": "percent",
                                                     "value": 100}, headers=ah)
    o = (await client.post("/api/v1/orders", json={"items": [{"product_id": await _milk(), "qty": 1}],
                                                   "coupon_code": "FREE1308"}, headers=referee)).json()
    await client.post("/api/v1/payments/create", json={"purpose": "order_payment", "order_id": o["id"]},
                      headers=referee)
    assert (await client.get("/api/v1/loyalty", headers=referrer)).json()["points_balance"] == 0


# ---------- R3-10: freeze holds while a later run for tomorrow is in progress ----------
async def test_freeze_holds_while_a_run_is_generating(client, monkeypatch):
    from app.core.config import settings
    from app.services.subscription import IST

    cust = await login(client, "9000001309")
    sid = (await client.post("/api/v1/subscriptions", json={"home_address": "1 Rd"},
                             headers=auth(cust))).json()["id"]
    monkeypatch.setattr(settings, "CUTOFF_HOUR", 0)
    tomorrow = dt.datetime.now(IST).date() + dt.timedelta(days=1)
    await _q("INSERT INTO delivery_schedule_runs (for_delivery_date, status, orders_generated, started_at, "
             "finished_at) VALUES (:d, 'frozen', 0, now() - interval '1 minute', now())", d=tomorrow)
    await _q("INSERT INTO delivery_schedule_runs (for_delivery_date, status, orders_generated, started_at) "
             "VALUES (:d, 'generating', 0, now())", d=tomorrow)
    try:
        assert (await client.post(f"/api/v1/subscriptions/{sid}/pause", headers=auth(cust))).status_code == 409
    finally:
        await _q("DELETE FROM delivery_schedule_runs WHERE for_delivery_date=:d AND orders_generated=0", d=tomorrow)


# ---------- R3-11: a coupon limit can't drop below what's already used ----------
async def test_coupon_limit_not_below_used(client):
    ah = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001310"))
    c = (await client.post("/api/v1/admin/coupons", json={"code": "LIM1310", "discount_type": "flat",
                                                          "value": 1}, headers=ah)).json()
    await client.post("/api/v1/orders", json={"items": [{"product_id": await _milk(), "qty": 2}],
                                              "coupon_code": "LIM1310"}, headers=h)
    r = await client.patch(f"/api/v1/admin/coupons/{c['id']}", json={"usage_limit": 0}, headers=ah)
    assert r.status_code == 422


# ---------- R3-12: long support threads show their NEWEST messages ----------
async def test_thread_cap_keeps_newest(client, monkeypatch):
    from app.api.v1 import support

    h = auth(await login(client, "9000001311"))
    t = (await client.post("/api/v1/support/tickets", json={"subject": "s", "body": "m0"}, headers=h)).json()
    for n in (1, 2, 3):
        await client.post(f"/api/v1/support/tickets/{t['id']}/messages", json={"body": f"m{n}"}, headers=h)
    monkeypatch.setattr(support, "_THREAD_CAP", 2)
    msgs = (await client.get(f"/api/v1/support/tickets/{t['id']}", headers=h)).json()["messages"]
    assert [m["body"] for m in msgs] == ["m2", "m3"]
