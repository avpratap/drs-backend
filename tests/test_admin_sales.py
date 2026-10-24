"""Admin sales/accounts/order-management grids."""
from conftest import auth, login


async def _seeded_product_id(client, h) -> int:
    prods = (await client.get("/api/v1/products", headers=h)).json()
    return next(p["id"] for p in prods if p["sku"] == "MILK-1L")


async def _place_order(client, ch) -> int:
    pid = await _seeded_product_id(client, ch)
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": 1}], "address": "12 MG Road"},
        headers=ch,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_admin_grids_return_and_gate(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    ch = auth(cust)
    await _place_order(client, ch)  # ensures an account + order exist

    assert (await client.get("/api/v1/admin/payments", headers=ah)).status_code == 200
    assert (await client.get("/api/v1/admin/refunds", headers=ah)).status_code == 200
    accts = await client.get("/api/v1/admin/accounts", headers=ah)
    assert accts.status_code == 200 and len(accts.json()) >= 1
    orders = await client.get("/api/v1/admin/orders", headers=ah)
    assert orders.status_code == 200 and len(orders.json()) >= 1

    # admin-gate: a customer is forbidden on every grid.
    for path in ("payments", "refunds", "accounts", "orders"):
        assert (await client.get(f"/api/v1/admin/{path}", headers=ch)).status_code == 403


async def test_admin_account_detail_aggregate(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    await _place_order(client, auth(cust))
    accts = (await client.get("/api/v1/admin/accounts?q=9876543210", headers=ah)).json()
    assert accts, "account should be searchable by owner phone"
    acct_id = accts[0]["id"]
    detail = (await client.get(f"/api/v1/admin/accounts/{acct_id}", headers=ah)).json()
    assert "wallet_balance" in detail and "credit_outstanding" in detail
    assert detail["order_count"] >= 1
    assert (await client.get("/api/v1/admin/accounts/999999", headers=ah)).status_code == 404


async def test_admin_order_cancel_then_conflict(client):
    import sqlalchemy as sa

    from app.core.db import engine

    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9100000972")  # own customer (coupon side effects below)
    await client.post("/api/v1/admin/coupons", json={
        "code": "ADMCANCEL972", "discount_type": "flat", "value": 5}, headers=ah)
    pid = await _seeded_product_id(client, auth(cust))
    oid = (await client.post("/api/v1/orders", json={
        "items": [{"product_id": pid, "qty": 2}], "coupon_code": "ADMCANCEL972"}, headers=auth(cust))).json()["id"]

    async def used():
        async with engine.connect() as conn:
            return (await conn.execute(sa.text("SELECT used_count FROM coupons WHERE code='ADMCANCEL972'"))).scalar()

    assert await used() == 1
    c = await client.post(f"/api/v1/admin/orders/{oid}/cancel", headers=ah)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert await used() == 0  # the coupon reservation is released on cancel
    again = await client.post(f"/api/v1/admin/orders/{oid}/cancel", headers=ah)
    assert again.status_code == 409


async def test_admin_payments_filter_by_account(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    ch = auth(cust)
    # A wallet top-up payment to list.
    await client.post(
        "/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 250}, headers=ch
    )
    accts = (await client.get("/api/v1/admin/accounts?q=9876543210", headers=ah)).json()
    acct_id = accts[0]["id"]
    pays = (await client.get(f"/api/v1/admin/payments?account_id={acct_id}", headers=ah)).json()
    assert any(p["account_id"] == acct_id and p["payment_type"] == "wallet_topup" for p in pays)
