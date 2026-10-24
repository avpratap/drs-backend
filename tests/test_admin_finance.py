from conftest import auth, driver_today, idem, login


async def _corner_store_id() -> int:
    import sqlalchemy as sa

    from app.core.db import engine

    async with engine.connect() as conn:
        return (await conn.execute(sa.text("SELECT id FROM shops WHERE name='Corner Store'"))).scalar()


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
    # Signed: a negative adjustment debits exactly that amount.
    down = await client.post(
        f"/api/v1/admin/accounts/{acct}/wallet/adjust",
        json={"amount": -100, "reason": "correction"},
        headers=idem(auth(admin)),
    )
    assert down.status_code == 200
    assert round(down.json()["wallet_balance"] - before, 2) == 150.0
    # zero rejected
    assert (await client.post(f"/api/v1/admin/accounts/{acct}/wallet/adjust", json={"amount": 0, "reason": "x"}, headers=idem(auth(admin)))).status_code == 422


async def test_admin_credit_settlement_reduces_outstanding(client, monkeypatch):
    """Deliver on a postpaid account to accrue credit, then settle part of it."""
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    delivery = await login(client, "9999900003")
    acct = await _subscriber_account(client, auth(admin), auth(sub))
    date = "2036-04-04"
    driver_today(monkeypatch, date)
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=auth(admin))
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=auth(delivery))).json()
    # The seeded Corner Store order specifically (the driver may be assigned other shops
    # by other tests, so orders[0] could be anyone's).
    corner = await _corner_store_id()
    order = next(o for o in orders if o["shop_id"] == corner)
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
    # Page through (the default page is 50 rows by id; the suite creates many shops).
    found, offset = False, 0
    while not found:
        page = (await client.get(f"/api/v1/admin/shops?limit=200&offset={offset}", headers=auth(admin))).json()
        if not page:
            break
        found = any(s["id"] == shop_id for s in page)
        offset += 200
    assert found
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


async def test_settle_credit_overpay_rejected(client):
    """AD6: a settlement larger than the outstanding credit is refused (no negative ledger)."""
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    delivery = await login(client, "9999900003")
    acct = await _subscriber_account(client, auth(admin), auth(sub))
    date = "2037-05-05"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=auth(admin))
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=auth(delivery))).json()
    # The seeded Corner Store order specifically (the driver may be assigned other shops
    # by other tests, so orders[0] could be anyone's).
    corner = await _corner_store_id()
    order = next(o for o in orders if o["shop_id"] == corner)
    item = order["items"][0]
    await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={"items": [{"product_id": item["product_id"], "qty_delivered": item["qty_planned"]}], "returns": []},
        headers=auth(delivery),
    )
    outstanding = (await client.get(f"/api/v1/admin/accounts/{acct}/billing", headers=auth(admin))).json()["credit_outstanding"]
    r = await client.post(
        f"/api/v1/admin/accounts/{acct}/credit/settle",
        json={"amount": outstanding + 100, "note": "overpay"},
        headers=idem(auth(admin)),
    )
    assert r.status_code == 422
    after = (await client.get(f"/api/v1/admin/accounts/{acct}/billing", headers=auth(admin))).json()
    assert after["credit_outstanding"] == outstanding  # the rejected settle wrote nothing


async def test_admin_cannot_mint_retail_customer(client):
    """AD8: admin provisioning is staff-only; retail customers self-register via OTP."""
    admin = await login(client, "9999900001")
    r = await client.post(
        "/api/v1/admin/users",
        json={"phone": "9112229999", "role": "customer"},
        headers=auth(admin),
    )
    assert r.status_code == 422
