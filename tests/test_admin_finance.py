from conftest import auth, idem, login


async def _subscriber_account(client, admin_h, sub_h) -> int:
    return (await client.get("/api/v1/billing/me", headers=sub_h)).json()["account_id"]


async def test_admin_account_billing_view(client):
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    acct = await _subscriber_account(client, auth(admin), auth(sub))
    r = await client.get(f"/api/v1/admin/accounts/{acct}/billing", headers=auth(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == acct and "wallet_balance" in body and "credit_outstanding" in body


async def test_admin_wallet_adjust_signed(client):
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    acct = await _subscriber_account(client, auth(admin), auth(sub))
    before = (await client.get(f"/api/v1/admin/accounts/{acct}/billing", headers=auth(admin))).json()["wallet_balance"]
    up = await client.post(
        f"/api/v1/admin/accounts/{acct}/wallet/adjust",
        json={"amount": 250, "reason": "goodwill"},
        headers=idem(auth(admin)),
    )
    assert up.status_code == 200
    assert round(up.json()["wallet_balance"] - before, 2) == 250.0
    # zero rejected
    assert (await client.post(f"/api/v1/admin/accounts/{acct}/wallet/adjust", json={"amount": 0, "reason": "x"}, headers=idem(auth(admin)))).status_code == 422


async def test_admin_credit_settlement_reduces_outstanding(client):
    """Deliver on a postpaid account to accrue credit, then settle part of it."""
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    delivery = await login(client, "9999900003")
    acct = await _subscriber_account(client, auth(admin), auth(sub))
    date = "2036-04-04"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=auth(admin))
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=auth(delivery))).json()
    order = orders[0]
    item = order["items"][0]
    await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={"items": [{"product_id": item["product_id"], "qty_delivered": item["qty_planned"]}], "returns": []},
        headers=auth(delivery),
    )
    outstanding = (await client.get(f"/api/v1/admin/accounts/{acct}/billing", headers=auth(admin))).json()["credit_outstanding"]
    assert outstanding > 0
    settle = await client.post(
        f"/api/v1/admin/accounts/{acct}/credit/settle",
        json={"amount": outstanding, "note": "NEFT recd"},
        headers=idem(auth(admin)),
    )
    assert settle.status_code == 200
    assert round(settle.json()["credit_outstanding"], 2) == 0.0


async def test_admin_shop_management(client):
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    shop_id = (await client.post("/api/v1/shops", json={"name": "Mgmt Shop"}, headers=auth(sub))).json()["id"]
    lst = await client.get("/api/v1/admin/shops", headers=auth(admin))
    assert any(s["id"] == shop_id for s in lst.json())
    upd = await client.patch(
        f"/api/v1/admin/shops/{shop_id}",
        json={"state": "Gujarat", "status": "inactive"},
        headers=auth(admin),
    )
    assert upd.status_code == 200 and upd.json()["state"] == "Gujarat" and upd.json()["status"] == "inactive"


async def test_admin_finance_requires_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/shops", headers=auth(cust))).status_code == 403
    assert (await client.post("/api/v1/admin/accounts/1/credit/settle", json={"amount": 1}, headers=idem(auth(cust)))).status_code == 403
