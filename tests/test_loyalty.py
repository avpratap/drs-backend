"""Tier-4 #36: loyalty points + referral + gift cards."""
from conftest import auth, ik, login


async def _account_id(client, h) -> int:
    return (await client.get("/api/v1/loyalty", headers=h)).json()["account_id"]


async def test_points_adjust_and_insufficient(client):
    admin = auth(await login(client, "9999900001"))
    cust = auth(await login(client, "9100001001"))
    aid = await _account_id(client, cust)

    r = await client.post(f"/api/v1/admin/loyalty/{aid}/adjust", json={"points": 100, "reason": "promo"}, headers=ik(admin))
    assert r.status_code == 200 and r.json()["points_balance"] == 100

    r2 = await client.post(f"/api/v1/admin/loyalty/{aid}/adjust", json={"points": -40}, headers=ik(admin))
    assert r2.json()["points_balance"] == 60

    over = await client.post(f"/api/v1/admin/loyalty/{aid}/adjust", json={"points": -100}, headers=ik(admin))
    assert over.status_code == 409

    mine = (await client.get("/api/v1/loyalty", headers=cust)).json()
    assert mine["points_balance"] == 60 and len(mine["recent"]) >= 2


async def test_referral_flow(client):
    referrer = auth(await login(client, "9100002001"))
    code1 = (await client.get("/api/v1/loyalty/referral-code", headers=referrer)).json()["code"]
    code2 = (await client.get("/api/v1/loyalty/referral-code", headers=referrer)).json()["code"]
    assert code1 == code2  # stable

    referee = auth(await login(client, "9100002002"))
    r = await client.post("/api/v1/loyalty/referral/redeem", json={"code": code1}, headers=referee)
    assert r.status_code == 200
    # No reward for a bare sign-up: the referrer is paid on the referee's first paid order.
    assert (await client.get("/api/v1/loyalty", headers=referrer)).json()["points_balance"] == 0
    prods = (await client.get("/api/v1/products", headers=referee)).json()
    milk = next(p["id"] for p in prods if p["sku"] == "MILK-1L")
    o = (await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}]},
                           headers=referee)).json()
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "order_payment", "order_id": o["id"]},
                             headers=referee)).json()
    await client.post("/api/v1/payments/verify", json={
        "gateway_order_id": pay["gateway_order_id"], "gateway_payment_id": "p", "signature": "dev"},
        headers=referee)
    assert (await client.get("/api/v1/loyalty", headers=referrer)).json()["points_balance"] == 100
    # A second paid order does not pay again.
    o2 = (await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}]},
                            headers=referee)).json()
    pay2 = (await client.post("/api/v1/payments/create", json={"purpose": "order_payment", "order_id": o2["id"]},
                              headers=referee)).json()
    await client.post("/api/v1/payments/verify", json={
        "gateway_order_id": pay2["gateway_order_id"], "gateway_payment_id": "p2", "signature": "dev"},
        headers=referee)
    assert (await client.get("/api/v1/loyalty", headers=referrer)).json()["points_balance"] == 100
    # Duplicate redemption by same referee -> 409.
    assert (await client.post("/api/v1/loyalty/referral/redeem", json={"code": code1}, headers=referee)).status_code == 409
    # Self-referral -> 409.
    assert (await client.post("/api/v1/loyalty/referral/redeem", json={"code": code1}, headers=referrer)).status_code == 409


async def test_gift_card_redeem(client):
    admin = auth(await login(client, "9999900001"))
    cust = auth(await login(client, "9100003001"))
    aid = await _account_id(client, cust)

    gc = (await client.post("/api/v1/admin/gift-cards", json={"amount": 500, "issued_to_account_id": aid}, headers=ik(admin))).json()
    code = gc["code"]
    assert gc["balance"] == 500.0

    listing = (await client.get("/api/v1/loyalty/gift-cards", headers=cust)).json()
    assert any(c["code"] == code for c in listing)

    async def wallet():
        return float((await client.get("/api/v1/billing/me", headers=cust)).json()["wallet_balance"])

    w0 = await wallet()
    r1 = await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": code, "amount": 200}, headers=cust)
    assert r1.status_code == 200 and r1.json()["balance"] == 300.0
    assert round(await wallet() - w0, 2) == 200.0  # the value LANDS in the wallet (M4)
    r2 = await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": code, "amount": 300}, headers=cust)
    assert r2.status_code == 200 and r2.json()["balance"] == 0.0 and r2.json()["status"] == "redeemed"
    assert round(await wallet() - w0, 2) == 500.0
    # Fully redeemed -> further redeem rejected.
    assert (await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": code, "amount": 1}, headers=cust)).status_code == 409


async def test_gift_card_over_redeem_and_expired(client):
    admin = auth(await login(client, "9999900001"))
    cust = auth(await login(client, "9100004001"))
    aid = await _account_id(client, cust)

    small = (await client.post("/api/v1/admin/gift-cards", json={"amount": 100, "issued_to_account_id": aid}, headers=ik(admin))).json()
    over = await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": small["code"], "amount": 150}, headers=cust)
    assert over.status_code == 422

    # Issuing a card that is ALREADY expired is rejected at issue time (422).
    r = await client.post(
        "/api/v1/admin/gift-cards",
        json={"amount": 100, "issued_to_account_id": aid, "expires_at": "2020-01-01"},
        headers=ik(admin),
    )
    assert r.status_code == 422


async def test_loyalty_admin_gate(client):
    cust = auth(await login(client, "9100005001"))
    assert (await client.post("/api/v1/admin/gift-cards", json={"amount": 100}, headers=cust)).status_code == 403
    assert (await client.post("/api/v1/admin/loyalty/1/adjust", json={"points": 10}, headers=ik(cust))).status_code == 403


async def test_gift_card_redeem_requires_key_and_retry_does_not_double_credit(client):
    """B3: a retried PARTIAL redeem with the same Idempotency-Key replays, so card value
    moves into the wallet once; a redeem without a key is refused (428)."""
    from conftest import NO_IDEM

    admin = auth(await login(client, "9999900001"))
    cust = auth(await login(client, "9100003002"))
    aid = await _account_id(client, cust)
    code = (await client.post("/api/v1/admin/gift-cards",
                              json={"amount": 500, "issued_to_account_id": aid}, headers=ik(admin))).json()["code"]
    url = "/api/v1/loyalty/gift-cards/redeem"

    async def wallet():
        return float((await client.get("/api/v1/billing/me", headers=cust)).json()["wallet_balance"])

    w0 = await wallet()
    r = await client.post(url, json={"code": code, "amount": 200}, headers={**cust, **NO_IDEM})
    assert r.status_code == 428
    assert await wallet() == w0  # nothing moved

    key = {**cust, "Idempotency-Key": "redeem-retry-1"}
    r1 = await client.post(url, json={"code": code, "amount": 200}, headers=key)
    r2 = await client.post(url, json={"code": code, "amount": 200}, headers=key)  # network retry
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json() and r2.json()["balance"] == 300.0
    assert round(await wallet() - w0, 2) == 200.0  # credited once, not twice
