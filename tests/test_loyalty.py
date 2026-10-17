"""Tier-4 #36: loyalty points + referral + gift cards."""
from conftest import auth, login


async def _account_id(client, h) -> int:
    return (await client.get("/api/v1/loyalty", headers=h)).json()["account_id"]


async def test_points_adjust_and_insufficient(client):
    admin = auth(await login(client, "9999900001"))
    cust = auth(await login(client, "9100001001"))
    aid = await _account_id(client, cust)

    r = await client.post(f"/api/v1/admin/loyalty/{aid}/adjust", json={"points": 100, "reason": "promo"}, headers=admin)
    assert r.status_code == 200 and r.json()["points_balance"] == 100

    r2 = await client.post(f"/api/v1/admin/loyalty/{aid}/adjust", json={"points": -40}, headers=admin)
    assert r2.json()["points_balance"] == 60

    over = await client.post(f"/api/v1/admin/loyalty/{aid}/adjust", json={"points": -100}, headers=admin)
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
    # Referrer awarded reward points.
    assert (await client.get("/api/v1/loyalty", headers=referrer)).json()["points_balance"] == 100
    # Duplicate redemption by same referee -> 409.
    assert (await client.post("/api/v1/loyalty/referral/redeem", json={"code": code1}, headers=referee)).status_code == 409
    # Self-referral -> 409.
    assert (await client.post("/api/v1/loyalty/referral/redeem", json={"code": code1}, headers=referrer)).status_code == 409


async def test_gift_card_redeem(client):
    admin = auth(await login(client, "9999900001"))
    cust = auth(await login(client, "9100003001"))
    aid = await _account_id(client, cust)

    gc = (await client.post("/api/v1/admin/gift-cards", json={"amount": 500, "issued_to_account_id": aid}, headers=admin)).json()
    code = gc["code"]
    assert gc["balance"] == 500.0

    listing = (await client.get("/api/v1/loyalty/gift-cards", headers=cust)).json()
    assert any(c["code"] == code for c in listing)

    r1 = await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": code, "amount": 200}, headers=cust)
    assert r1.status_code == 200 and r1.json()["balance"] == 300.0
    r2 = await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": code, "amount": 300}, headers=cust)
    assert r2.status_code == 200 and r2.json()["balance"] == 0.0 and r2.json()["status"] == "redeemed"
    # Fully redeemed -> further redeem rejected.
    assert (await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": code, "amount": 1}, headers=cust)).status_code == 409


async def test_gift_card_over_redeem_and_expired(client):
    admin = auth(await login(client, "9999900001"))
    cust = auth(await login(client, "9100004001"))
    aid = await _account_id(client, cust)

    small = (await client.post("/api/v1/admin/gift-cards", json={"amount": 100, "issued_to_account_id": aid}, headers=admin)).json()
    over = await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": small["code"], "amount": 150}, headers=cust)
    assert over.status_code == 422

    expired = (await client.post(
        "/api/v1/admin/gift-cards",
        json={"amount": 100, "issued_to_account_id": aid, "expires_at": "2020-01-01"},
        headers=admin,
    )).json()
    r = await client.post("/api/v1/loyalty/gift-cards/redeem", json={"code": expired["code"], "amount": 10}, headers=cust)
    assert r.status_code == 409


async def test_loyalty_admin_gate(client):
    cust = auth(await login(client, "9100005001"))
    assert (await client.post("/api/v1/admin/gift-cards", json={"amount": 100}, headers=cust)).status_code == 403
    assert (await client.post("/api/v1/admin/loyalty/1/adjust", json={"points": 10}, headers=cust)).status_code == 403
