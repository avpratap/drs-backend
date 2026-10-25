"""Customer-facing API gaps for the storefront: public serviceability, the wallet
transaction ledger, and the B2B credit statement."""
import sqlalchemy as sa
from conftest import auth, idem, login

from app.core.db import SessionLocal, engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _account_id(client, h) -> int:
    await client.get("/api/v1/loyalty", headers=h)  # ensure the account exists
    return (await client.get("/api/v1/billing/me", headers=h)).json()["account_id"]


# ---------- 1) serviceability works before login ----------
async def test_serviceability_is_public_and_cached(client, monkeypatch):
    from app.services import shipping

    calls = []

    async def fake(pincode, weight_kg, cod):
        calls.append((pincode, weight_kg, cod))
        return {"serviceable": True, "etd_days": 2, "courier": "X", "rate": 40.0}

    monkeypatch.setattr(shipping, "check_serviceability", fake)
    r = await client.get("/api/v1/shipping/serviceability?pincode=302001&weight_kg=1")  # no auth
    assert r.status_code == 200 and r.json()["serviceable"] is True and r.json()["etd_days"] == 2
    again = await client.get("/api/v1/shipping/serviceability?pincode=302001&weight_kg=1")
    assert again.json() == r.json() and len(calls) == 1  # cached: one courier call
    cod = await client.get("/api/v1/shipping/serviceability?pincode=302001&weight_kg=1&cod=true")
    assert cod.status_code == 200 and len(calls) == 2  # different question, new lookup
    # Still works with a token, and input stays validated.
    h = auth(await login(client, "9000001501"))
    assert (await client.get("/api/v1/shipping/serviceability?pincode=302001", headers=h)).status_code == 200
    assert (await client.get("/api/v1/shipping/serviceability?pincode=12")).status_code == 422
    assert (await client.get("/api/v1/shipping/serviceability?pincode=302001&weight_kg=0")).status_code == 422


# ---------- 2) wallet transaction ledger ----------
async def test_wallet_transactions_paginated_and_scoped(client):
    from app.services import billing

    h = auth(await login(client, "9000001502"))
    other = auth(await login(client, "9000001503"))
    acct = await _account_id(client, h)
    other_acct = await _account_id(client, other)
    async with SessionLocal() as db:
        for amt in (100, -30, 50, -20, 10):
            await billing.wallet_adjust(db, acct, amt, "topup" if amt > 0 else "debit_order")
        await billing.wallet_adjust(db, other_acct, 999, "topup")
        await db.commit()
    r = await client.get("/api/v1/billing/transactions?limit=2", headers=h)
    assert r.status_code == 200 and r.headers["x-total-count"] == "5"
    rows = r.json()
    assert [x["amount"] for x in rows] == [10.0, -20.0]  # newest first
    assert rows[0]["balance_after"] == 110.0 and {"id", "txn_type", "created_at"} <= rows[0].keys()
    rest = (await client.get("/api/v1/billing/transactions?limit=10&offset=2", headers=h)).json()
    assert [x["amount"] for x in rest] == [50.0, -30.0, 100.0]
    # Only the caller's own account (the other user's 999 never appears).
    assert all(x["amount"] != 999.0 for x in rows + rest)
    assert (await client.get("/api/v1/billing/transactions")).status_code in (401, 403)  # needs auth


async def test_wallet_transactions_empty_for_new_user(client):
    h = auth(await login(client, "9000001504"))
    r = await client.get("/api/v1/billing/transactions", headers=h)
    assert r.status_code == 200 and r.json() == [] and r.headers["x-total-count"] == "0"


# ---------- 3) B2B credit statement ----------
async def test_credit_statement_lines_for_credit_terms_account(client):
    from app.services import billing

    admin = auth(await login(client, "9999900001"))
    h = auth(await login(client, "9000001505"))
    acct = await _account_id(client, h)
    await client.patch(f"/api/v1/admin/accounts/{acct}/type",
                       json={"account_type": "b2b", "billing_mode": "postpaid_credit"}, headers=admin)
    async with SessionLocal() as db:
        await billing.credit_entry(db, acct, 500, "charge", "test", None)
        await billing.credit_entry(db, acct, 300, "charge", "test", None)
        await db.commit()
    settle = await client.post(f"/api/v1/admin/accounts/{acct}/credit/settle", json={"amount": 200},
                               headers=idem(admin))
    assert settle.status_code == 200, settle.text
    r = await client.get("/api/v1/billing/credit-statement", headers=h)
    assert r.status_code == 200 and r.headers["x-total-count"] == "3"
    lines = r.json()
    assert [(x["entry_type"], x["amount"], x["balance_after"]) for x in lines] == [
        ("payment", -200.0, 600.0), ("charge", 300.0, 800.0), ("charge", 500.0, 500.0)]
    page2 = (await client.get("/api/v1/billing/credit-statement?limit=1&offset=1", headers=h)).json()
    assert [x["amount"] for x in page2] == [300.0]
    # The running balance matches the scalar outstanding exposed on /billing/me.
    me = (await client.get("/api/v1/billing/me", headers=h)).json()
    assert me["credit_outstanding"] == lines[0]["balance_after"]
    # Another customer sees none of it.
    other = auth(await login(client, "9000001506"))
    assert (await client.get("/api/v1/billing/credit-statement", headers=other)).json() == []


# ---------- 4) customer writes require + honour an Idempotency-Key ----------
async def _milk_id() -> int:
    return (await _q("SELECT id FROM products WHERE sku='MILK-1L'"))[0][0]


async def test_order_create_requires_key_and_replays(client):
    from conftest import NO_IDEM

    h = auth(await login(client, "9000001510"))
    body = {"items": [{"product_id": await _milk_id(), "qty": 2}]}
    assert (await client.post("/api/v1/orders", json=body, headers={**h, **NO_IDEM})).status_code == 428
    key = {**h, "Idempotency-Key": "order-1510-a"}
    first = await client.post("/api/v1/orders", json=body, headers=key)
    again = await client.post("/api/v1/orders", json=body, headers=key)  # storefront retry
    assert first.status_code == again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    acct = (await _q("SELECT a.id FROM customer_accounts a JOIN users u ON u.id=a.owner_user_id "
                     "WHERE u.phone='9000001510'"))[0][0]
    assert (await _q("SELECT count(*) FROM orders WHERE account_id=:a", a=acct))[0][0] == 1
    # Same key, different body: refused, never a silent second order.
    other = await client.post("/api/v1/orders", json={"items": [{"product_id": await _milk_id(), "qty": 3}]},
                              headers=key)
    assert other.status_code == 422
    assert (await _q("SELECT count(*) FROM orders WHERE account_id=:a", a=acct))[0][0] == 1


async def test_checkout_requires_key_and_replays(client):
    from conftest import NO_IDEM

    h = auth(await login(client, "9000001511"))
    await client.post("/api/v1/cart/items", json={"product_id": await _milk_id(), "qty": 1}, headers=h)
    assert (await client.post("/api/v1/cart/checkout", json={}, headers={**h, **NO_IDEM})).status_code == 428
    key = {**h, "Idempotency-Key": "checkout-1511"}
    first = await client.post("/api/v1/cart/checkout", json={}, headers=key)
    again = await client.post("/api/v1/cart/checkout", json={}, headers=key)
    assert first.status_code == again.status_code == 200, first.text
    assert again.json()["id"] == first.json()["id"]
    acct = (await _q("SELECT a.id FROM customer_accounts a JOIN users u ON u.id=a.owner_user_id "
                     "WHERE u.phone='9000001511'"))[0][0]
    assert (await _q("SELECT count(*) FROM orders WHERE account_id=:a", a=acct))[0][0] == 1


async def test_payment_create_requires_key_and_replays(client):
    from conftest import NO_IDEM

    h = auth(await login(client, "9000001512"))
    body = {"purpose": "wallet_topup", "amount": 100}
    assert (await client.post("/api/v1/payments/create", json=body, headers={**h, **NO_IDEM})).status_code == 428
    key = {**h, "Idempotency-Key": "pay-1512"}
    first = await client.post("/api/v1/payments/create", json=body, headers=key)
    again = await client.post("/api/v1/payments/create", json=body, headers=key)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()  # same payment / gateway order replayed
    acct = (await _q("SELECT a.id FROM customer_accounts a JOIN users u ON u.id=a.owner_user_id "
                     "WHERE u.phone='9000001512'"))[0][0]
    assert (await _q("SELECT count(*) FROM payments WHERE account_id=:a", a=acct))[0][0] == 1
    # A different key is a deliberately new top-up.
    new = await client.post("/api/v1/payments/create", json=body, headers={**h, "Idempotency-Key": "pay-1512-b"})
    assert new.status_code == 200 and new.json()["payment_id"] != first.json()["payment_id"]
