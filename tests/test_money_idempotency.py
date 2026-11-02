"""Handler-level idempotency on admin money endpoints with no natural state guard.

Scenario: the handler commits the money movement, then the process dies before the
middleware stores the response; the reaper later deletes the orphaned claim. A retry
with the same Idempotency-Key then reaches the handler again — it must replay the
stored result, never move the money a second time."""
import uuid

import pytest
from conftest import auth, login
from sqlalchemy import delete

from app.core.db import SessionLocal
from app.models.idempotency import IdempotencyKey


async def _reap_claim(raw_key: str) -> None:
    """What the maintenance reaper does to an orphaned claim (row gone entirely)."""
    async with SessionLocal() as db:
        await db.execute(delete(IdempotencyKey).where(IdempotencyKey.key.like(f"%:{raw_key}")))
        await db.commit()


async def _account(client, admin, *, credit=False) -> tuple[int, dict]:
    h = auth(await login(client, f"7{uuid.uuid4().int % 10**9:09d}"))
    await client.get("/api/v1/loyalty", headers=h)  # ensure the account exists
    acct = (await client.get("/api/v1/billing/me", headers=h)).json()["account_id"]
    if credit:
        await client.patch(f"/api/v1/admin/accounts/{acct}/type",
                           json={"account_type": "b2b", "billing_mode": "postpaid_credit"},
                           headers=admin)
    return acct, h


async def _wallet(client, h) -> float:
    return (await client.get("/api/v1/billing/me", headers=h)).json()["wallet_balance"]


@pytest.mark.parametrize("endpoint", ["topup", "adjust"])
async def test_wallet_retry_after_reaped_claim_does_not_double_apply(client, endpoint):
    admin = auth(await login(client, "9999900001"))
    acct, h = await _account(client, admin)
    key = f"k-{uuid.uuid4().hex}"
    hdr = {**admin, "Idempotency-Key": key}
    if endpoint == "topup":
        url, body = "/api/v1/billing/admin/topup", {"account_id": acct, "amount": 150}
    else:
        url, body = f"/api/v1/admin/accounts/{acct}/wallet/adjust", {"amount": 150, "reason": "goodwill"}

    r1 = await client.post(url, json=body, headers=hdr)
    assert r1.status_code == 200, r1.text
    await _reap_claim(key)
    r2 = await client.post(url, json=body, headers=hdr)  # retry reaches the handler again
    assert r2.status_code == 200 and r2.json() == r1.json()
    assert await _wallet(client, h) == 150.0  # moved once

    # Same key, different request after the reap: refused, nothing moved.
    await _reap_claim(key)
    other = {**body, "amount": 999}
    assert (await client.post(url, json=other, headers=hdr)).status_code == 422
    assert await _wallet(client, h) == 150.0


async def test_giftcard_redeem_retry_after_reaped_claim_does_not_double_credit(client):
    """The crown-jewel fix: a PARTIAL gift-card redeem whose claim was reaped after the
    handler committed must REPLAY — never debit the card or credit the wallet twice."""
    admin = auth(await login(client, "9999900001"))
    acct, h = await _account(client, admin)
    issue_hdr = {**admin, "Idempotency-Key": f"gi-{uuid.uuid4().hex}"}
    code = (await client.post("/api/v1/admin/gift-cards",
                              json={"amount": 500, "issued_to_account_id": acct},
                              headers=issue_hdr)).json()["code"]
    assert await _wallet(client, h) == 0.0

    key = f"gr-{uuid.uuid4().hex}"
    hdr = {**h, "Idempotency-Key": key}
    r1 = await client.post("/api/v1/loyalty/gift-cards/redeem",
                           json={"code": code, "amount": 200}, headers=hdr)
    assert r1.status_code == 200, r1.text
    assert r1.json()["balance"] == 300.0 and await _wallet(client, h) == 200.0

    await _reap_claim(key)  # crash window
    r2 = await client.post("/api/v1/loyalty/gift-cards/redeem",
                           json={"code": code, "amount": 200}, headers=hdr)
    assert r2.status_code == 200 and r2.json() == r1.json()
    assert r2.json()["balance"] == 300.0  # card debited once
    assert await _wallet(client, h) == 200.0  # wallet credited once

    # Same key, different amount after a reap: refused, nothing moves.
    await _reap_claim(key)
    assert (await client.post("/api/v1/loyalty/gift-cards/redeem",
                              json={"code": code, "amount": 50}, headers=hdr)).status_code == 422
    assert await _wallet(client, h) == 200.0


async def test_giftcard_issue_retry_after_reaped_claim_does_not_duplicate(client):
    admin = auth(await login(client, "9999900001"))
    key = f"gi-{uuid.uuid4().hex}"
    hdr = {**admin, "Idempotency-Key": key}
    r1 = await client.post("/api/v1/admin/gift-cards", json={"amount": 500}, headers=hdr)
    assert r1.status_code == 200, r1.text
    await _reap_claim(key)
    r2 = await client.post("/api/v1/admin/gift-cards", json={"amount": 500}, headers=hdr)
    assert r2.status_code == 200 and r2.json() == r1.json()  # the SAME card, not a second one


async def test_points_adjust_retry_after_reaped_claim_does_not_double_apply(client):
    admin = auth(await login(client, "9999900001"))
    acct, _h = await _account(client, admin)
    key = f"pa-{uuid.uuid4().hex}"
    hdr = {**admin, "Idempotency-Key": key}
    url = f"/api/v1/admin/loyalty/{acct}/adjust"
    r1 = await client.post(url, json={"points": 100, "reason": "goodwill"}, headers=hdr)
    assert r1.status_code == 200, r1.text
    await _reap_claim(key)
    r2 = await client.post(url, json={"points": 100, "reason": "goodwill"}, headers=hdr)
    assert r2.status_code == 200 and r2.json()["points_balance"] == r1.json()["points_balance"]


async def test_credit_settle_retry_after_reaped_claim_does_not_double_apply(client):
    from app.services import billing

    admin = auth(await login(client, "9999900001"))
    acct, _h = await _account(client, admin, credit=True)
    async with SessionLocal() as db:
        await billing.credit_entry(db, acct, 500, "charge", "test", None)
        await db.commit()
    key = f"k-{uuid.uuid4().hex}"
    hdr = {**admin, "Idempotency-Key": key}
    url = f"/api/v1/admin/accounts/{acct}/credit/settle"
    r1 = await client.post(url, json={"amount": 200}, headers=hdr)
    assert r1.status_code == 200 and r1.json()["credit_outstanding"] == 300.0
    await _reap_claim(key)
    r2 = await client.post(url, json={"amount": 200}, headers=hdr)
    assert r2.status_code == 200 and r2.json() == r1.json()
    async with SessionLocal() as db:
        assert await billing.credit_outstanding(db, acct) == 300.0  # settled once
