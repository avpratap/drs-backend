from conftest import auth, login

from app.services.forecasting import forecast_variable


def test_forecast_regular_uses_ewma():
    fc = forecast_variable([10, 12, 11, 13, 12, 14, 12])
    assert fc["method"] == "ewma"
    assert fc["point"] > 0
    assert fc["safety"] >= 0


def test_forecast_intermittent_uses_croston():
    fc = forecast_variable([0, 0, 5, 0, 0, 0, 4, 0, 0, 0])
    assert fc["method"] == "croston"
    assert 0 < fc["point"] < 5  # demand-rate deflated by sparsity


def test_forecast_empty_is_zero():
    assert forecast_variable([])["method"] == "none"
    assert forecast_variable([0, 0, 0])["point"] == 0.0


async def test_forecast_endpoint_combines_committed_and_variable(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9100000970")  # own customer: demand attributable to this test

    async def p1():
        body = (await client.get("/api/v1/admin/forecast", headers=auth(admin))).json()
        return next((row for row in body["plan"] if row["product_id"] == 1), None)

    before = await p1()
    committed_before = before["committed_qty"] if before else 0.0
    # Add a known standing order (7/day) on a fresh shop: committed must rise by exactly 7
    # (absolute totals depend on every other test's subscriptions in the shared DB).
    shop = (await client.post("/api/v1/shops", json={"name": "Forecast shop"}, headers=auth(admin))).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=auth(admin))).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": 1, "default_qty": 7, "frequency": "daily"}], headers=auth(admin))
    # And some one-time (variable) demand today.
    for qty in (3, 2):
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": 1, "qty": qty}]}, headers=auth(cust)
        )
    after = await p1()
    assert round(after["committed_qty"] - committed_before, 3) == 7.0
    assert after["forecast_variable_qty"] > 0  # real demand was just recorded
    assert after["recommended_production"] >= after["committed_qty"] + after["forecast_variable_qty"]
    assert after["method"] in ("ewma", "croston")


async def test_forecast_requires_admin(client):
    cust = await login(client, "9876543210")
    r = await client.get("/api/v1/admin/forecast", headers=auth(cust))
    assert r.status_code == 403
