from conftest import auth, driver_today, idem, login


async def _corner_store_id() -> int:
    import sqlalchemy as sa

    from app.core.db import engine

    async with engine.connect() as conn:
        return (await conn.execute(sa.text("SELECT id FROM shops WHERE name='Corner Store'"))).scalar()


async def _account(client, token):
    return (await client.get("/api/v1/billing/me", headers=auth(token))).json()


async def test_admin_topup_increases_wallet(client):
    admin = await login(client, "9999900001")
    subscriber = await login(client, "9999900002")  # seeded account exists
    before = await _account(client, subscriber)
    acct_id = before["account_id"]

    r = await client.post(
        "/api/v1/billing/admin/topup",
        json={"account_id": acct_id, "amount": 500},
        headers=idem(auth(admin)),
    )
    assert r.status_code == 200
    after = await _account(client, subscriber)
    assert round(after["wallet_balance"] - before["wallet_balance"], 2) == 500.0


async def test_non_admin_topup_forbidden(client):
    subscriber = await login(client, "9999900002")
    acct_id = (await _account(client, subscriber))["account_id"]
    r = await client.post(
        "/api/v1/billing/admin/topup",
        json={"account_id": acct_id, "amount": 10},
        headers=idem(auth(subscriber)),
    )
    assert r.status_code == 403


async def test_customer_cannot_deliver(client):
    cust = await login(client, "9876543210")
    r = await client.post(
        "/api/v1/delivery/orders/1/deliver",
        json={"items": [{"product_id": 1, "qty_delivered": 0}], "returns": []},
        headers=auth(cust),
    )
    assert r.status_code == 403


async def test_delivery_unknown_order_404(client):
    delivery = await login(client, "9999900003")
    r = await client.post(
        "/api/v1/delivery/orders/999999/deliver",
        json={"items": [{"product_id": 1, "qty_delivered": 0}], "returns": []},
        headers=auth(delivery),
    )
    assert r.status_code == 404


async def test_credit_billing_net_of_returns(client, monkeypatch):
    """Postpaid delivery charges credit = delivered - returned, idempotently."""
    admin = await login(client, "9999900001")
    delivery = await login(client, "9999900003")
    subscriber = await login(client, "9999900002")
    date = "2031-09-09"
    driver_today(monkeypatch, date)

    run = await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=auth(admin))
    assert run.json()["orders_generated"] >= 1
    orders = (
        await client.get(f"/api/v1/delivery/orders?date={date}", headers=auth(delivery))
    ).json()
    # The seeded Corner Store order specifically (the driver may be assigned other shops
    # by other tests, so orders[0] could be anyone's).
    corner = await _corner_store_id()
    order = next(o for o in orders if o["shop_id"] == corner)
    item = order["items"][0]
    unit = item["unit_price"]

    before = (await _account(client, subscriber))["credit_outstanding"]
    # Deliver full, return 1 unit -> charge (qty-1)*unit.
    deliver = await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={
            "items": [{"product_id": item["product_id"], "qty_delivered": item["qty_planned"]}],
            "returns": [{"product_id": item["product_id"], "qty_returned": 1, "reason": "damaged"}],
        },
        headers=auth(delivery),
    )
    assert deliver.status_code == 200
    after = (await _account(client, subscriber))["credit_outstanding"]
    expected = round((float(item["qty_planned"]) - 1) * float(unit), 2)
    assert round(after - before, 2) == expected
