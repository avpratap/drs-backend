from conftest import auth, login


async def test_rate_product_upsert_and_aggregate(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    r1 = await client.post("/api/v1/products/1/ratings", json={"stars": 5, "comment": "great"}, headers=h)
    assert r1.status_code == 200 and r1.json()["stars"] == 5
    agg = (await client.get("/api/v1/products/1/ratings")).json()
    assert agg["count"] >= 1 and agg["average"] >= 1
    # Re-rate -> upsert (count unchanged for this user), value updates.
    r2 = await client.post("/api/v1/products/1/ratings", json={"stars": 3}, headers=h)
    assert r2.json()["id"] == r1.json()["id"] and r2.json()["stars"] == 3


async def test_rating_validation(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    assert (await client.post("/api/v1/products/1/ratings", json={"stars": 6}, headers=h)).status_code == 422
    assert (await client.post("/api/v1/products/1/ratings", json={"stars": 0}, headers=h)).status_code == 422
    assert (await client.post("/api/v1/products/999999/ratings", json={"stars": 4}, headers=h)).status_code == 404


async def test_withdrawal_request_and_admin_approval(client):
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    acct = (await client.get("/api/v1/billing/me", headers=auth(sub))).json()["account_id"]
    await client.post("/api/v1/billing/admin/topup", json={"account_id": acct, "amount": 1000}, headers=auth(admin))
    bal0 = (await client.get("/api/v1/billing/me", headers=auth(sub))).json()["wallet_balance"]

    req = await client.post("/api/v1/billing/withdrawals", json={"amount": 400}, headers=auth(sub))
    assert req.status_code == 200 and req.json()["status"] == "requested"
    wd_id = req.json()["id"]
    # Over balance rejected.
    assert (await client.post("/api/v1/billing/withdrawals", json={"amount": 9_000_000}, headers=auth(sub))).status_code == 422

    assert any(w["id"] == wd_id for w in (await client.get("/api/v1/admin/withdrawals", headers=auth(admin))).json())
    ok = await client.post(f"/api/v1/admin/withdrawals/{wd_id}/approve", headers=auth(admin))
    assert ok.status_code == 200 and ok.json()["status"] == "paid"
    bal1 = (await client.get("/api/v1/billing/me", headers=auth(sub))).json()["wallet_balance"]
    assert round(bal0 - bal1, 2) == 400.0
    # Double-approve blocked.
    assert (await client.post(f"/api/v1/admin/withdrawals/{wd_id}/approve", headers=auth(admin))).status_code == 409


async def test_withdrawal_reject(client):
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    acct = (await client.get("/api/v1/billing/me", headers=auth(sub))).json()["account_id"]
    await client.post("/api/v1/billing/admin/topup", json={"account_id": acct, "amount": 50}, headers=auth(admin))
    wd_id = (await client.post("/api/v1/billing/withdrawals", json={"amount": 20}, headers=auth(sub))).json()["id"]
    rej = await client.post(f"/api/v1/admin/withdrawals/{wd_id}/reject", headers=auth(admin))
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"


async def test_withdrawal_admin_gate(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/withdrawals", headers=auth(cust))).status_code == 403
