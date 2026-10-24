from conftest import auth, idem, login


async def test_rate_product_upsert_and_aggregate(client):
    import uuid

    admin = auth(await login(client, "9999900001"))
    cust = await login(client, "9876543210")
    h = auth(cust)
    # A FRESH product so the aggregate is exactly this user's rating (product 1 collects
    # ratings from every other test).
    pid = (await client.post("/api/v1/products", json={"sku": f"RT-{uuid.uuid4().hex[:8]}", "name": "rt"},
                             headers=admin)).json()["id"]

    async def rate(stars):
        r = await client.post(f"/api/v1/products/{pid}/ratings", json={"stars": stars, "comment": "c"}, headers=h)
        if r.json()["status"] == "pending":  # unverified → moderated (O8)
            await client.post(f"/api/v1/admin/ratings/{r.json()['id']}/moderate",
                              json={"status": "approved"}, headers=admin)
        return r

    r1 = await rate(5)
    assert r1.status_code == 200 and r1.json()["stars"] == 5
    agg = (await client.get(f"/api/v1/products/{pid}/ratings")).json()
    assert agg["count"] == 1 and agg["average"] == 5.0
    # Re-rate -> upsert (still ONE rating for this user), value updates.
    r2 = await rate(3)
    assert r2.json()["id"] == r1.json()["id"] and r2.json()["stars"] == 3
    agg = (await client.get(f"/api/v1/products/{pid}/ratings")).json()
    assert agg["count"] == 1 and agg["average"] == 3.0


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
    await client.post("/api/v1/billing/admin/topup", json={"account_id": acct, "amount": 1000}, headers=idem(auth(admin)))
    bal0 = (await client.get("/api/v1/billing/me", headers=auth(sub))).json()["wallet_balance"]

    req = await client.post("/api/v1/billing/withdrawals", json={"amount": 400}, headers=auth(sub))
    assert req.status_code == 200 and req.json()["status"] == "requested"
    wd_id = req.json()["id"]
    # Over balance rejected.
    assert (await client.post("/api/v1/billing/withdrawals", json={"amount": 9_000_000}, headers=auth(sub))).status_code == 422

    # The pending request reserves its amount: another request beyond the remainder fails.
    assert (await client.post("/api/v1/billing/withdrawals", json={"amount": round(bal0 - 400 + 1, 2)},
                              headers=auth(sub))).status_code == 422
    listed = (await client.get("/api/v1/admin/withdrawals?limit=200", headers=auth(admin))).json()
    assert any(w["id"] == wd_id for w in listed)
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
    await client.post("/api/v1/billing/admin/topup", json={"account_id": acct, "amount": 50}, headers=idem(auth(admin)))
    bal = (await client.get("/api/v1/billing/me", headers=auth(sub))).json()["wallet_balance"]
    wd_id = (await client.post("/api/v1/billing/withdrawals", json={"amount": 20}, headers=auth(sub))).json()["id"]
    rej = await client.post(f"/api/v1/admin/withdrawals/{wd_id}/reject", headers=auth(admin))
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    # Nothing was debited, and the reservation is released: the full balance can be
    # requested again.
    assert (await client.get("/api/v1/billing/me", headers=auth(sub))).json()["wallet_balance"] == bal
    again = await client.post("/api/v1/billing/withdrawals", json={"amount": bal}, headers=auth(sub))
    assert again.status_code == 200
    await client.post(f"/api/v1/admin/withdrawals/{again.json()['id']}/reject", headers=auth(admin))


async def test_withdrawal_admin_gate(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/withdrawals", headers=auth(cust))).status_code == 403
