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
    cust = await login(client, "9876543210")

    # Generate some one-time (variable) demand for product 1 today.
    for qty in (3, 2):
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": 1, "qty": qty}]}, headers=auth(cust)
        )

    r = await client.get("/api/v1/admin/forecast", headers=auth(admin))
    assert r.status_code == 200
    body = r.json()
    assert body.get("plan")
    p1 = next(row for row in body["plan"] if row["product_id"] == 1)
    # Seeded daily subscription (qty 5) is the known committed base.
    assert p1["committed_qty"] == 5.0
    assert p1["forecast_variable_qty"] >= 0
    assert p1["recommended_production"] >= p1["committed_qty"]
    assert p1["method"] in ("ewma", "croston", "none")


async def test_forecast_requires_admin(client):
    cust = await login(client, "9876543210")
    r = await client.get("/api/v1/admin/forecast", headers=auth(cust))
    assert r.status_code == 403
