"""Regression tests for the full-backend review: money/ledger (M*) and GST (G*) fixes.
IDs refer to scratchpad/MASTER_FIXLIST.md."""
import sqlalchemy as sa
from conftest import auth, login

from app.core.db import engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _pid(client, token, sku) -> int:
    prods = (await client.get("/api/v1/products", headers=auth(token))).json()
    return next(p["id"] for p in prods if p["sku"] == sku)


async def _order(client, token, pid, qty, **extra) -> dict:
    r = await client.post(
        "/api/v1/orders", json={"items": [{"product_id": pid, "qty": qty}], **extra},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _create_pay(client, token, oid) -> dict:
    r = await client.post(
        "/api/v1/payments/create", json={"purpose": "order_payment", "order_id": oid},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _verify(client, token, goid, sig="dev"):
    return await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "pay_x", "signature": sig},
        headers=auth(token),
    )


async def _paid(client, token, pid, qty) -> tuple[int, int]:
    o = await _order(client, token, pid, qty)
    pay = await _create_pay(client, token, o["id"])
    assert (await _verify(client, token, pay["gateway_order_id"])).status_code == 200
    return o["id"], pay["payment_id"]


async def _wallet(client, token) -> float:
    await client.get("/api/v1/loyalty", headers=auth(token))  # ensure the account exists
    return float((await client.get("/api/v1/billing/me", headers=auth(token))).json()["wallet_balance"])


async def _topup(client, token, amount) -> int:
    pay = (await client.post(
        "/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": amount},
        headers=auth(token),
    )).json()
    assert (await _verify(client, token, pay["gateway_order_id"])).status_code == 200
    return pay["payment_id"]


# ---------- M2: failed attempt / bad signature must not lose a later capture ----------
async def test_bad_signature_does_not_fail_payment_and_capture_still_lands(client):
    cust = await login(client, "9000000801")
    pid = await _pid(client, cust, "MILK-1L")
    o = await _order(client, cust, pid, 1)
    pay = await _create_pay(client, cust, o["id"])
    bad = await _verify(client, cust, pay["gateway_order_id"], sig="garbage")
    assert bad.status_code == 400
    assert (await _q("SELECT status FROM payments WHERE id=:i", i=pay["payment_id"]))[0][0] == "initiated"
    # Webhook: a failed ATTEMPT, then the retry is captured on the same gateway order.
    await client.post("/api/v1/payments/webhook", json={
        "event": "payment.failed", "id": "evt_fail_801", "order_id": pay["gateway_order_id"]})
    assert (await _q("SELECT status FROM payments WHERE id=:i", i=pay["payment_id"]))[0][0] == "initiated"
    cap = await client.post("/api/v1/payments/webhook", json={
        "event": "payment.captured", "id": "evt_cap_801", "order_id": pay["gateway_order_id"],
        "payment_id": "pay_ok"})
    assert cap.json()["status"] == "ok"
    order = (await client.get(f"/api/v1/orders/{o['id']}", headers=auth(cust))).json()
    assert order["payment_status"] == "paid" and order["status"] == "confirmed"


# ---------- M5/O1: a cancelled order is not payable and never resurrected ----------
async def test_cancelled_order_cannot_be_paid(client):
    cust = await login(client, "9000000802")
    pid = await _pid(client, cust, "MILK-1L")
    o = await _order(client, cust, pid, 1)
    assert (await client.post(f"/api/v1/orders/{o['id']}/cancel", headers=auth(cust))).status_code == 200
    r = await client.post(
        "/api/v1/payments/create", json={"purpose": "order_payment", "order_id": o["id"]},
        headers=auth(cust),
    )
    assert r.status_code == 409


async def test_capture_after_cancel_keeps_order_cancelled(client):
    cust = await login(client, "9000000803")
    pid = await _pid(client, cust, "MILK-1L")
    o = await _order(client, cust, pid, 1)
    pay = await _create_pay(client, cust, o["id"])
    assert (await client.post(f"/api/v1/orders/{o['id']}/cancel", headers=auth(cust))).status_code == 200
    assert (await _verify(client, cust, pay["gateway_order_id"])).status_code == 200
    order = (await client.get(f"/api/v1/orders/{o['id']}", headers=auth(cust))).json()
    # Captured money is recorded (so it gets refunded) but the order stays cancelled.
    assert order["status"] == "cancelled" and order["payment_status"] == "paid"


# ---------- M10/O9: a fully-discounted order settles without the gateway ----------
async def test_zero_total_order_settles_without_gateway(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000000804")
    await client.post("/api/v1/admin/coupons", json={
        "code": "FREE804", "discount_type": "percent", "value": 100}, headers=auth(admin))
    pid = await _pid(client, cust, "MILK-1L")
    o = await _order(client, cust, pid, 1, coupon_code="FREE804")
    assert o["total"] == 0
    pay = await _create_pay(client, cust, o["id"])
    assert pay["amount"] == 0 and pay["gateway_order_id"].startswith("free_")
    order = (await client.get(f"/api/v1/orders/{o['id']}", headers=auth(cust))).json()
    assert order["payment_status"] == "paid" and order["status"] == "confirmed"
    # Nothing was charged → nothing to refund.
    r = await client.post(f"/api/v1/payments/{pay['payment_id']}/refund", json={}, headers=auth(admin))
    assert r.status_code == 409


# ---------- M1/M7: unknown-outcome partial refund blocks retries until reconciled ----------
async def test_pending_refund_blocks_new_refund_and_reconcile_applies_once(client, monkeypatch):
    from app.services import payments as pay_service

    admin = await login(client, "9999900001")
    cust = await login(client, "9000000805")
    pid_ = await _topup(client, cust, 300)
    before = await _wallet(client, cust)

    async def timeout(*a, **k):
        raise TimeoutError("gateway timed out")

    monkeypatch.setattr(pay_service, "create_gateway_refund", timeout)
    r = await client.post(f"/api/v1/payments/{pid_}/refund", json={"amount": 100}, headers=auth(admin))
    assert r.status_code == 502
    [(rid, status)] = await _q("SELECT id, status FROM refunds WHERE payment_id=:p", p=pid_)
    assert status == "pending"
    monkeypatch.undo()
    # The blind retry (same partial amount) must NOT issue a second gateway refund.
    retry = await client.post(f"/api/v1/payments/{pid_}/refund", json={"amount": 100}, headers=auth(admin))
    assert retry.status_code == 409
    # Admin confirms the gateway did refund: ledger effect applied exactly once.
    rec = await client.post(f"/api/v1/payments/refunds/{rid}/reconcile",
                            json={"outcome": "processed"}, headers=auth(admin))
    assert rec.status_code == 200 and rec.json()["status"] == "processed"
    assert await _wallet(client, cust) == round(before - 100, 2)
    again = await client.post(f"/api/v1/payments/refunds/{rid}/reconcile",
                              json={"outcome": "processed"}, headers=auth(admin))
    assert again.status_code == 200 and await _wallet(client, cust) == round(before - 100, 2)
    assert (await client.post(f"/api/v1/payments/refunds/{rid}/reconcile",
                              json={"outcome": "failed"}, headers=auth(admin))).status_code == 409
    # Reconcile is admin-only.
    assert (await client.post(f"/api/v1/payments/refunds/{rid}/reconcile",
                              json={"outcome": "processed"}, headers=auth(cust))).status_code == 403


async def test_failed_reconcile_frees_the_cap(client, monkeypatch):
    from app.services import payments as pay_service

    admin = await login(client, "9999900001")
    cust = await login(client, "9000000806")
    pid_ = await _topup(client, cust, 200)

    async def boom(*a, **k):
        raise TimeoutError("x")

    monkeypatch.setattr(pay_service, "create_gateway_refund", boom)
    assert (await client.post(f"/api/v1/payments/{pid_}/refund", json={}, headers=auth(admin))).status_code == 502
    monkeypatch.undo()
    [(rid,)] = await _q("SELECT id FROM refunds WHERE payment_id=:p", p=pid_)
    await client.post(f"/api/v1/payments/refunds/{rid}/reconcile", json={"outcome": "failed"}, headers=auth(admin))
    ok = await client.post(f"/api/v1/payments/{pid_}/refund", json={}, headers=auth(admin))
    assert ok.status_code == 200 and ok.json()["amount"] == 200.0


# ---------- M8: a spent top-up can't also be refunded to the card ----------
async def test_topup_refund_capped_at_wallet_balance(client):
    from app.core.db import SessionLocal
    from app.services import billing

    admin = await login(client, "9999900001")
    cust = await login(client, "9000000807")
    pid_ = await _topup(client, cust, 200)
    acct = (await _q("SELECT account_id FROM payments WHERE id=:p", p=pid_))[0][0]
    async with SessionLocal() as db:  # customer spends 150 of it
        await billing.wallet_adjust(db, acct, -150, "debit_order")
        await db.commit()
    assert (await client.post(f"/api/v1/payments/{pid_}/refund", json={"amount": 100},
                              headers=auth(admin))).status_code == 422
    r = await client.post(f"/api/v1/payments/{pid_}/refund", json={}, headers=auth(admin))
    assert r.status_code == 200 and r.json()["amount"] == 50.0
    assert await _wallet(client, cust) == 0.0


# ---------- M4/M12: gift cards credit the wallet; issued cards are private ----------
async def test_gift_card_redeem_credits_wallet_and_balance_is_private(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000000808")
    other = await login(client, "9000000809")
    await client.get("/api/v1/loyalty", headers=auth(cust))
    acct = (await _q(
        "SELECT a.id FROM customer_accounts a JOIN users u ON u.id=a.owner_user_id WHERE u.phone='9000000808'"
    ))[0][0]
    gc = (await client.post("/api/v1/admin/gift-cards",
                            json={"amount": 250, "issued_to_account_id": acct}, headers=auth(admin))).json()
    code = gc["code"]
    assert (await client.get(f"/api/v1/loyalty/gift-cards/{code}/balance", headers=auth(other))).status_code == 404
    assert (await client.get(f"/api/v1/loyalty/gift-cards/{code}/balance", headers=auth(cust))).status_code == 200
    before = await _wallet(client, cust)
    r = await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": code, "amount": 100},
                          headers=auth(cust))
    assert r.status_code == 200 and float(r.json()["balance"]) == 150.0
    assert await _wallet(client, cust) == round(before + 100, 2)


# ---------- M14: one referral per referee ----------
async def test_referee_cannot_farm_multiple_referral_codes(client):
    a = await login(client, "9000000810")
    b = await login(client, "9000000811")
    ref = await login(client, "9000000812")
    code_a = (await client.get("/api/v1/loyalty/referral-code", headers=auth(a))).json()["code"]
    code_b = (await client.get("/api/v1/loyalty/referral-code", headers=auth(b))).json()["code"]
    assert (await client.post("/api/v1/loyalty/referral/redeem", json={"code": code_a},
                              headers=auth(ref))).status_code == 200
    assert (await client.post("/api/v1/loyalty/referral/redeem", json={"code": code_b},
                              headers=auth(ref))).status_code == 409


# ---------- M9: an over-long Idempotency-Key is a 400, not a 500 ----------
async def test_long_idempotency_key_rejected(client):
    cust = await login(client, "9000000813")
    r = await client.post(
        "/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 10},
        headers={**auth(cust), "Idempotency-Key": "k" * 300},
    )
    assert r.status_code == 400


# ---------- M15: wallet txn amount and balance move are the same paisa ----------
async def test_wallet_adjust_quantizes_amount(client):
    from app.core.db import SessionLocal
    from app.services import billing

    await client.get("/api/v1/loyalty", headers=auth(await login(client, "9000000814")))
    acct = (await _q(
        "SELECT a.id FROM customer_accounts a JOIN users u ON u.id=a.owner_user_id WHERE u.phone='9000000814'"
    ))[0][0]
    async with SessionLocal() as db:
        await billing.wallet_adjust(db, acct, 0.125, "adjustment")
        await db.commit()
    [(amt, after)] = await _q(
        "SELECT t.amount, t.balance_after FROM wallet_transactions t JOIN wallets w ON w.id=t.wallet_id "
        "WHERE w.account_id=:a ORDER BY t.id DESC LIMIT 1", a=acct)
    assert float(amt) == float(after)  # both 0.13 (or both 0.12) — never 0.13 vs 0.12


# ---------- G1/G4/G11: repeated RMA refunds accumulate on ONE consistent credit note ----------
async def test_two_rma_refunds_share_one_consistent_credit_note(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000000815")
    ghee = await _pid(client, cust, "GHEE-1KG")  # ₹500 @12% inclusive
    oid, _ = await _paid(client, cust, ghee, 3)
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()
    assert len(inv["lines"]) == 1  # G8: the order-invoice endpoint returns its lines

    async def rma(qty, amount, split=False):
        items = ([{"product_id": ghee, "qty": qty / 2}, {"product_id": ghee, "qty": qty / 2}]
                 if split else [{"product_id": ghee, "qty": qty}])
        rid = (await client.post("/api/v1/returns", json={"order_id": oid, "items": items},
                                 headers=auth(cust))).json()["id"]
        await client.post(f"/api/v1/admin/returns/{rid}/approve", headers=auth(admin))
        return await client.post(f"/api/v1/admin/returns/{rid}/refund", json={"amount": amount},
                                 headers=auth(admin))

    assert (await rma(1, 500)).status_code == 200
    assert (await rma(1, 500, split=True)).status_code == 200  # was a 500 (2nd CN insert)
    cns = await _q("SELECT id, total, is_partial FROM invoices WHERE original_invoice_id=:i", i=inv["id"])
    assert len(cns) == 1
    cn_id, total, partial = cns[0]
    assert float(total) == -1000.0 and partial is True
    lines = await _q(
        "SELECT qty, taxable_value, cgst, sgst, igst, line_total FROM invoice_lines WHERE invoice_id=:i",
        i=cn_id)
    assert sum(float(q) for q, *_ in lines) == -2.0  # both split lines of RMA #2 reversed
    for _qty, tv, c, s, i, lt in lines:
        assert round(float(tv) + float(c) + float(s) + float(i), 2) == float(lt)
    # Header equals the sum of its lines.
    assert round(sum(float(lt) for *_, lt in lines), 2) == float(total)


async def test_partial_credit_note_never_drifts_a_paisa(client):
    from app.core.db import SessionLocal
    from app.models.invoice import Invoice
    from app.services import invoicing

    admin = await login(client, "9999900001")
    cust = await login(client, "9000000816")
    ghee = await _pid(client, cust, "GHEE-1KG")
    oid, _ = await _paid(client, cust, ghee, 3)
    inv_id = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()["id"]
    async with SessionLocal() as db:
        inv = await db.get(Invoice, inv_id)
        note = await invoicing.generate_credit_note_for_lines(db, inv, {ghee: 1}, "t", amount=333.33)
        await db.commit()
        assert float(note.total) == -333.33
        assert round(float(note.taxable_value) + float(note.cgst) + float(note.sgst)
                     + float(note.igst), 2) == -333.33
    assert admin


# ---------- G6: partial refund to source issues a value-only credit note ----------
async def test_partial_refund_to_source_issues_value_credit_note(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000000817")
    ghee = await _pid(client, cust, "GHEE-1KG")
    oid, pay_id = await _paid(client, cust, ghee, 3)
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()
    r = await client.post(f"/api/v1/payments/{pay_id}/refund", json={"amount": 300}, headers=auth(admin))
    assert r.status_code == 200
    [(cn_id, total)] = await _q("SELECT id, total FROM invoices WHERE original_invoice_id=:i", i=inv["id"])
    assert float(total) == -300.0
    assert all(float(q) == 0 for (q,) in await _q("SELECT qty FROM invoice_lines WHERE invoice_id=:i", i=cn_id))
    # The remainder refund upgrades it to the exact full reversal.
    rest = await client.post(f"/api/v1/payments/{pay_id}/refund", json={}, headers=auth(admin))
    assert rest.status_code == 200 and rest.json()["amount"] == 1200.0
    [(total2, partial2)] = await _q("SELECT total, is_partial FROM invoices WHERE id=:i", i=cn_id)
    assert float(total2) == -1500.0 and partial2 is False


# ---------- G5: place of supply compares canonical GST state codes ----------
def test_state_normalisation():
    from app.core.gst_states import canonical, same_state, state_code

    for v in ("RJ", "rajasthan", "Rajasthan.", "08", "8", " RAJASTHAN "):
        assert state_code(v) == "08" and same_state(v, "Rajasthan")
    assert not same_state("MH", "Rajasthan") and same_state("Maharashtra", "mh")
    assert canonical("tamil nadu") == "Tamil Nadu" and canonical("Atlantis") == "Atlantis"
    assert same_state("J&K", "Jammu and Kashmir")


async def test_abbreviated_home_state_is_intra_state(client):
    cust = await login(client, "9000000818")
    pid = await _pid(client, cust, "GHEE-1KG")
    o = await _order(client, cust, pid, 1, address="1 MI Road", state="RJ")
    pay = await _create_pay(client, cust, o["id"])
    await _verify(client, cust, pay["gateway_order_id"])
    inv = (await client.post(f"/api/v1/orders/{o['id']}/invoice", headers=auth(cust))).json()
    assert inv["is_interstate"] is False and inv["igst"] == 0 and inv["cgst"] > 0
    assert inv["place_of_supply"] == "Rajasthan"


# ---------- G13: invoice prefix can't overflow invoice_no ----------
def test_invoice_prefix_length_validated():
    import pytest

    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(INVOICE_PREFIX="X" * 12)


async def _delivered_order(client, admin_h, items, date, returns_all=False):
    shop_id = (await client.post("/api/v1/shops", json={"name": f"G9 {date}", "state": "Rajasthan"},
                                 headers=admin_h)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=admin_h)).json()["id"]
    r = await client.put(f"/api/v1/subscriptions/{sid}/items", json=[
        {"product_id": pid, "default_qty": q, "frequency": "daily"} for pid, q in items
    ], headers=admin_h)
    assert r.status_code == 200, r.text
    await client.post(f"/api/v1/admin/cutoff/run?date={date}&force=true", json={}, headers=admin_h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=admin_h)).json()
    order = next(o for o in orders if o["shop_id"] == shop_id)
    lines = [{"product_id": i["product_id"], "qty_delivered": i["qty_planned"]} for i in order["items"]]
    returns = ([{"product_id": i["product_id"], "qty_returned": i["qty_planned"], "reason": "refused"}
                for i in order["items"]] if returns_all else [])
    d = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver",
                          json={"items": lines, "returns": returns}, headers=admin_h)
    assert d.status_code == 200, d.text
    return order["id"]


async def test_delivery_billing_equals_invoice_total_to_the_paisa(client):
    """G9: billing rounded the SUM once while the invoice rounds per line."""
    admin = await login(client, "9999900001")
    h = auth(admin)
    milk = await _pid(client, admin, "MILK-1L")
    ghee = await _pid(client, admin, "GHEE-1KG")
    oid = await _delivered_order(client, h, [(milk, 1.555), (ghee, 0.007)], "2038-03-03")
    billed = (await _q("SELECT total FROM delivery_orders WHERE id=:i", i=oid))[0][0]
    inv = (await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={oid}", headers=h)).json()
    assert float(billed) == inv["total"]
    assert inv["total"] == round(sum(ln["line_total"] for ln in inv["lines"]), 2)


async def test_fully_returned_delivery_issues_no_invoice(client):
    """G10: a zero-value invoice must not burn a gap-free GST serial."""
    admin = await login(client, "9999900001")
    h = auth(admin)
    milk = await _pid(client, admin, "MILK-1L")
    oid = await _delivered_order(client, h, [(milk, 2)], "2038-03-04", returns_all=True)
    before = (await _q("SELECT COALESCE(MAX(seq_no),0) FROM invoices WHERE NOT is_credit_note"))[0][0]
    r = await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={oid}", headers=h)
    assert r.status_code == 409
    after = (await _q("SELECT COALESCE(MAX(seq_no),0) FROM invoices WHERE NOT is_credit_note"))[0][0]
    assert after == before
