from conftest import auth, login


async def test_export_my_data(client):
    # Fresh user so we don't pollute the shared customer's address book.
    phone = "9998887001"
    cust = await login(client, phone)
    await client.patch("/api/v1/profile", json={"display_name": "Exportee"}, headers=auth(cust))
    await client.post(
        "/api/v1/addresses",
        json={"line1": "1 Rd", "city": "Jaipur", "state": "Rajasthan", "pincode": "302001"},
        headers=auth(cust),
    )
    r = await client.get("/api/v1/account/export", headers=auth(cust))
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["phone"] == phone
    assert body["profile"]["display_name"] == "Exportee"
    assert isinstance(body["addresses"], list) and body["addresses"]
    assert "orders" in body and "invoices" in body and "consents" in body


async def test_erase_my_account(client):
    # Use the 'other' customer so we don't disturb the main test customer.
    other = await login(client, "9191919191")
    await client.patch("/api/v1/profile", json={"display_name": "Zap", "email": "z@x.com"}, headers=auth(other))
    r = await client.post("/api/v1/account/erase", headers=auth(other))
    assert r.status_code == 200
    # Token is invalidated (token_version bumped) -> old token now rejected.
    me = await client.get("/api/v1/auth/me", headers=auth(other))
    assert me.status_code == 401


async def test_admin_user_management(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    cid = (await client.get("/api/v1/profile", headers=auth(cust))).json()["user_id"]
    h = auth(admin)

    lst = await client.get("/api/v1/admin/users?role=delivery", headers=h)
    assert lst.status_code == 200
    assert all(u["role"] == "delivery" for u in lst.json())

    # Promote the customer (resolved id, never the admin) to subscriber, then restore.
    patched = await client.patch(f"/api/v1/admin/users/{cid}", json={"role": "subscriber"}, headers=h)
    assert patched.status_code == 200 and patched.json()["role"] == "subscriber"
    await client.patch(f"/api/v1/admin/users/{cid}", json={"role": "customer"}, headers=h)

    bad = await client.patch(f"/api/v1/admin/users/{cid}", json={"role": "wizard"}, headers=h)
    assert bad.status_code == 422


async def test_reports_summary(client):
    admin = await login(client, "9999900001")
    r = await client.get("/api/v1/admin/reports/summary", headers=auth(admin))
    assert r.status_code == 200
    body = r.json()
    for key in (
        "paid_order_revenue",
        "wallet_float",
        "credit_outstanding",
        "active_subscriptions",
        "shops",
        "delivered_orders",
    ):
        assert key in body


async def test_reports_requires_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/reports/summary", headers=auth(cust))).status_code == 403
