"""Regression tests for the second review round: refunds, credit notes, locking (R2-M*).
IDs refer to scratchpad/MASTER_FIXLIST.md."""
import sqlalchemy as sa
from conftest import auth, ik, login

from app.core.db import engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _pid(client, token, sku) -> int:
    prods = (await client.get("/api/v1/products", headers=auth(token))).json()
    return next(p["id"] for p in prods if p["sku"] == sku)


async def _verify(client, token, goid):
    return await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "pay_x", "signature": "dev"},
        headers=auth(token),
    )


async def _paid_order(client, token, items) -> tuple[int, int]:
    o = (await client.post("/api/v1/orders", json={"items": items}, headers=auth(token))).json()
    pay = (await client.post("/api/v1/payments/create", json={
        "purpose": "order_payment", "order_id": o["id"]}, headers=auth(token))).json()
    assert (await _verify(client, token, pay["gateway_order_id"])).status_code == 200
    return o["id"], pay["payment_id"]


async def _topup(client, token, amount) -> int:
    pay = (await client.post("/api/v1/payments/create", json={
        "purpose": "wallet_topup", "amount": amount}, headers=auth(token))).json()
    assert (await _verify(client, token, pay["gateway_order_id"])).status_code == 200
    return pay["payment_id"]


async def _wallet(client, token) -> float:
    return float((await client.get("/api/v1/billing/me", headers=auth(token))).json()["wallet_balance"])


async def _refund(client, admin, pay_id, **body):
    return await client.post(f"/api/v1/payments/{pay_id}/refund", json=body, headers=ik(auth(admin)))


# ---------- R2-M1: a duplicate capture is refunded on its own ----------
async def test_duplicate_capture_refund_leaves_order_and_invoice_alone(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001001")
    milk = await _pid(client, cust, "MILK-1L")
    oid, pay_a = await _paid_order(client, cust, [{"product_id": milk, "qty": 2}])
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()
    acct = (await _q("SELECT account_id FROM payments WHERE id=:p", p=pay_a))[0][0]
    # A second capture on the same order (e.g. a legacy failed gateway order captured late).
    await _q("INSERT INTO payments (account_id, payment_type, gateway, gateway_order_id, amount, status, "
             "ref_type, ref_id) VALUES (:a, 'order_payment', 'razorpay', 'order_dup1001', 54, 'initiated', "
             "'order', :o)", a=acct, o=oid)
    assert (await _verify(client, cust, "order_dup1001")).status_code == 200
    [(pay_b, excess)] = await _q("SELECT id, is_excess FROM payments WHERE gateway_order_id='order_dup1001'")
    assert excess is True
    r = await _refund(client, admin, pay_b)
    assert r.status_code == 200 and r.json()["amount"] == 54.0
    order = (await client.get(f"/api/v1/orders/{oid}", headers=auth(cust))).json()
    assert order["payment_status"] == "paid"
    assert not await _q("SELECT 1 FROM invoices WHERE original_invoice_id=:i", i=inv["id"])
    # The order's own payment is still fully refundable.
    full = await _refund(client, admin, pay_a)
    assert full.status_code == 200 and full.json()["amount"] == 54.0


# ---------- R2-M2: a refund settled by reconcile during the gateway call isn't re-applied ----------
async def test_refund_settled_during_gateway_call_is_not_applied_twice(client, monkeypatch):
    from app.services import payments as pay_service

    admin = await login(client, "9999900001")
    cust = await login(client, "9000001002")
    milk = await _pid(client, cust, "MILK-1L")
    oid, pay_id = await _paid_order(client, cust, [{"product_id": milk, "qty": 2}])

    async def settled_meanwhile(*a, **k):
        # A concurrent reconcile applied the effect and committed while we waited.
        await _q("UPDATE refunds SET status='processed', ledger_applied_at=now() WHERE payment_id=:p",
                 p=pay_id)
        return "rfnd_x"

    monkeypatch.setattr(pay_service, "create_gateway_refund", settled_meanwhile)
    r = await _refund(client, admin, pay_id)
    assert r.status_code == 200 and r.json()["status"] == "processed"
    # refund_payment saw the applied marker and did NOT apply the order effect again.
    order = (await client.get(f"/api/v1/orders/{oid}", headers=auth(cust))).json()
    assert order["payment_status"] == "paid"


async def test_gateway_error_never_overwrites_a_reconciled_status(client, monkeypatch):
    from app.services import payments as pay_service

    admin = await login(client, "9999900001")
    cust = await login(client, "9000001003")
    milk = await _pid(client, cust, "MILK-1L")
    _, pay_id = await _paid_order(client, cust, [{"product_id": milk, "qty": 1}])

    async def settled_then_timeout(*a, **k):
        await _q("UPDATE refunds SET status='processed', ledger_applied_at=now() WHERE payment_id=:p",
                 p=pay_id)
        raise TimeoutError("late")

    monkeypatch.setattr(pay_service, "create_gateway_refund", settled_then_timeout)
    assert (await _refund(client, admin, pay_id)).status_code == 409
    assert (await _q("SELECT status FROM refunds WHERE payment_id=:p", p=pay_id))[0][0] == "processed"


# ---------- R2-M3: an in-flight 'initiated' refund can't be reconciled 'failed' ----------
async def test_young_initiated_refund_cannot_be_reconciled(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001004")
    milk = await _pid(client, cust, "MILK-1L")
    _, pay_id = await _paid_order(client, cust, [{"product_id": milk, "qty": 1}])
    acct = (await _q("SELECT account_id FROM payments WHERE id=:p", p=pay_id))[0][0]
    [(rid,)] = await _q("INSERT INTO refunds (payment_id, account_id, amount, status) "
                        "VALUES (:p, :a, 27, 'initiated') RETURNING id", p=pay_id, a=acct)
    url = f"/api/v1/payments/refunds/{rid}/reconcile"
    assert (await client.post(url, json={"outcome": "failed"}, headers=auth(admin))).status_code == 409
    await _q("UPDATE refunds SET created_at = now() - interval '11 minutes' WHERE id=:i", i=rid)
    r = await client.post(url, json={"outcome": "failed"}, headers=auth(admin))
    assert r.status_code == 200 and r.json()["status"] == "failed"


# ---------- R2-M4: top-up refund holds the money before the gateway call ----------
async def test_topup_refund_holds_wallet_during_gateway_call(client, monkeypatch):
    from app.services import payments as pay_service

    admin = await login(client, "9999900001")
    cust = await login(client, "9000001005")
    pay_id = await _topup(client, cust, 300)
    seen = {}

    async def gateway(*a, **k):
        acct = (await _q("SELECT account_id FROM payments WHERE id=:p", p=pay_id))[0][0]
        seen["balance"] = float((await _q("SELECT balance FROM wallets WHERE account_id=:a", a=acct))[0][0])
        raise TimeoutError("x")

    monkeypatch.setattr(pay_service, "create_gateway_refund", gateway)
    assert (await _refund(client, admin, pay_id, amount=100)).status_code == 502
    assert seen["balance"] == 200.0  # held: not spendable/withdrawable while in flight
    assert await _wallet(client, cust) == 200.0
    [(rid,)] = await _q("SELECT id FROM refunds WHERE payment_id=:p", p=pay_id)
    # Reconciled 'failed' → the hold is released; 'processed' later would not debit twice.
    await client.post(f"/api/v1/payments/refunds/{rid}/reconcile", json={"outcome": "failed"},
                      headers=auth(admin))
    assert await _wallet(client, cust) == 300.0
    monkeypatch.undo()
    ok = await _refund(client, admin, pay_id, amount=100)
    assert ok.status_code == 200 and await _wallet(client, cust) == 200.0


async def test_refund_requires_idempotency_key_and_is_audited(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001006")
    pay_id = await _topup(client, cust, 50)
    r = await client.post(f"/api/v1/payments/{pay_id}/refund", json={}, headers=auth(admin))
    assert r.status_code == 428
    assert (await _refund(client, admin, pay_id)).status_code == 200
    rows = await _q("SELECT detail FROM admin_audit_log WHERE action='payment_refund' "
                    "AND resource_id=:p", p=str(pay_id))
    assert len(rows) == 1


# ---------- R2-M5: RMA after a partial refund to source: CN matches the money ----------
async def test_rma_after_partial_refund_reverses_shortfall_and_closes_order(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001007")
    milk = await _pid(client, cust, "MILK-1L")
    ghee = await _pid(client, cust, "GHEE-1KG")
    oid, pay_id = await _paid_order(client, cust, [{"product_id": ghee, "qty": 1},
                                                   {"product_id": milk, "qty": 10}])
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()
    assert inv["total"] == 770.0
    assert (await _refund(client, admin, pay_id, amount=400)).status_code == 200
    await _q("UPDATE orders SET status='delivered' WHERE id=:o", o=oid)
    rid = (await client.post("/api/v1/returns", json={
        "order_id": oid, "items": [{"product_id": milk, "qty": 10}]}, headers=auth(cust))).json()["id"]
    await client.post(f"/api/v1/admin/returns/{rid}/approve", headers=auth(admin))
    rr = await client.post(f"/api/v1/admin/returns/{rid}/refund", json={"amount": 270}, headers=auth(admin))
    assert rr.status_code == 200, rr.text
    [(total, partial)] = await _q("SELECT total, is_partial FROM invoices WHERE original_invoice_id=:i",
                                  i=inv["id"])
    assert float(total) == -670.0 and partial is True  # 400 to source + 270 store credit
    # The last ₹100 to source closes the order and fully reverses the invoice.
    assert (await _refund(client, admin, pay_id)).json()["amount"] == 100.0
    order = (await client.get(f"/api/v1/orders/{oid}", headers=auth(cust))).json()
    assert order["payment_status"] == "refunded"
    [(total, partial)] = await _q("SELECT total, is_partial FROM invoices WHERE original_invoice_id=:i",
                                  i=inv["id"])
    assert float(total) == -770.0 and partial is False


async def test_rma_that_exhausts_headroom_marks_order_refunded(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001008")
    milk = await _pid(client, cust, "MILK-1L")
    oid, pay_id = await _paid_order(client, cust, [{"product_id": milk, "qty": 4}])
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()
    assert (await _refund(client, admin, pay_id, amount=54)).status_code == 200
    await _q("UPDATE orders SET status='delivered' WHERE id=:o", o=oid)
    rid = (await client.post("/api/v1/returns", json={
        "order_id": oid, "items": [{"product_id": milk, "qty": 2}]}, headers=auth(cust))).json()["id"]
    await client.post(f"/api/v1/admin/returns/{rid}/approve", headers=auth(admin))
    assert (await client.post(f"/api/v1/admin/returns/{rid}/refund", json={"amount": 54},
                              headers=auth(admin))).status_code == 200
    order = (await client.get(f"/api/v1/orders/{oid}", headers=auth(cust))).json()
    assert order["payment_status"] == "refunded"
    [(total, partial)] = await _q("SELECT total, is_partial FROM invoices WHERE original_invoice_id=:i",
                                  i=inv["id"])
    assert float(total) == -108.0 and partial is False
    # Fully refunded → admin can now cancel it (was stuck at 'partially_refunded').
    assert order["payment_status"] != "partially_refunded"


# ---------- R2-M6: credit-note allocation never yields a negative / over-cap line ----------
async def test_credit_note_lines_are_never_negative(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001009")
    ah = auth(admin)
    tier = (await _q("SELECT id FROM price_tiers WHERE code='RETAIL'"))[0][0]
    pids = []
    for n in range(7):
        pid = (await client.post("/api/v1/products", json={"sku": f"CN7-{n}", "name": f"cn{n}"},
                                 headers=ah)).json()["id"]
        await _q("INSERT INTO product_prices (product_id, price_tier_id, unit_price, min_qty, valid_from) "
                 "VALUES (:p, :t, 1, 1, '2025-01-01')", p=pid, t=tier)
        pids.append(pid)
    oid, pay_id = await _paid_order(client, cust, [{"product_id": p, "qty": 1} for p in pids])
    inv = (await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))).json()
    assert (await _refund(client, admin, pay_id, amount=0.04)).status_code == 200
    lines = await _q("SELECT l.line_total FROM invoice_lines l JOIN invoices i ON i.id=l.invoice_id "
                     "WHERE i.original_invoice_id=:i", i=inv["id"])
    assert all(float(lt) <= 0 for (lt,) in lines)
    assert round(sum(float(lt) for (lt,) in lines), 2) == -0.04


# ---------- R2-M7: refunded-before-invoice orders still get invoice + CN; free orders none ----------
async def test_partially_refunded_order_gets_invoice_and_credit_note(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001010")
    milk = await _pid(client, cust, "MILK-1L")
    oid, pay_id = await _paid_order(client, cust, [{"product_id": milk, "qty": 2}])
    assert (await _refund(client, admin, pay_id, amount=10)).status_code == 200
    r = await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))
    assert r.status_code == 200 and r.json()["total"] == 54.0
    [(total,)] = await _q("SELECT total FROM invoices WHERE original_invoice_id=:i", i=r.json()["id"])
    assert float(total) == -10.0
    again = await client.post(f"/api/v1/orders/{oid}/invoice", headers=auth(cust))
    assert again.json()["id"] == r.json()["id"]
    [(total,)] = await _q("SELECT total FROM invoices WHERE original_invoice_id=:i", i=r.json()["id"])
    assert float(total) == -10.0  # idempotent: no second catch-up


async def test_free_order_gets_no_zero_invoice(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001011")
    await client.post("/api/v1/admin/coupons", json={
        "code": "FREE1011", "discount_type": "percent", "value": 100}, headers=auth(admin))
    milk = await _pid(client, cust, "MILK-1L")
    o = (await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}],
                                                   "coupon_code": "FREE1011"}, headers=auth(cust))).json()
    await client.post("/api/v1/payments/create", json={"purpose": "order_payment", "order_id": o["id"]},
                      headers=auth(cust))
    assert (await client.post(f"/api/v1/orders/{o['id']}/invoice", headers=auth(cust))).status_code == 409
    assert not await _q("SELECT 1 FROM invoices WHERE order_id=:o", o=o["id"])


# ---------- R2-M8: a locked read sees rows committed after an earlier read ----------
async def test_locked_select_refreshes_identity_map():
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models.billing import Wallet

    async with SessionLocal() as s:
        w = (await s.execute(select(Wallet).order_by(Wallet.id).limit(1))).scalar_one()
        wid, old = w.id, float(w.balance)
        await s.commit()
        await _q("UPDATE wallets SET balance = balance + 7 WHERE id=:i", i=wid)
        try:
            locked = (await s.execute(select(Wallet).where(Wallet.id == wid).with_for_update())).scalar_one()
            assert float(locked.balance) == round(old + 7, 2)
        finally:
            await s.rollback()
            await _q("UPDATE wallets SET balance = balance - 7 WHERE id=:i", i=wid)


# ---------- R2-M9: only cash paid in can be withdrawn (not gift-card/store credit) ----------
async def test_gift_card_credit_cannot_be_withdrawn_as_cash(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9000001012")
    await _topup(client, cust, 100)
    acct = (await client.get("/api/v1/billing/me", headers=auth(cust))).json()["account_id"]
    gc = (await client.post("/api/v1/admin/gift-cards", json={"amount": 500, "issued_to_account_id": acct},
                            headers=ik(auth(admin)))).json()
    assert (await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": gc["code"], "amount": 500},
                              headers=auth(cust))).status_code == 200
    assert await _wallet(client, cust) == 600.0
    over = await client.post("/api/v1/billing/withdrawals", json={"amount": 101}, headers=auth(cust))
    assert over.status_code == 422
    ok = await client.post("/api/v1/billing/withdrawals", json={"amount": 100}, headers=auth(cust))
    assert ok.status_code == 200


# ---------- R2-M10: every admin write leaves an audit row in the same transaction ----------
async def test_admin_writes_are_audited_automatically(client):
    admin = await login(client, "9999900001")
    r = await client.post("/api/v1/admin/coupons", json={
        "code": "AUD1013", "discount_type": "flat", "value": 5}, headers=auth(admin))
    assert r.status_code == 200
    rows = await _q("SELECT action, detail FROM admin_audit_log WHERE action='create_coupon' "
                    "AND detail->'body'->>'code' = 'AUD1013'")
    assert len(rows) == 1
    # A rejected write leaves no row.
    bad = await client.post("/api/v1/admin/coupons", json={
        "code": "AUD1013", "discount_type": "flat", "value": 5}, headers=auth(admin))
    assert bad.status_code in (409, 422)
    assert len(await _q("SELECT 1 FROM admin_audit_log WHERE action='create_coupon' "
                        "AND detail->'body'->>'code' = 'AUD1013'")) == 1
    listing = (await client.get("/api/v1/admin/audit-log", headers=auth(admin))).json()
    assert "created_at" in listing[0] and "detail" in listing[0]
