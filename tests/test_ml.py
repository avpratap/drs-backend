"""Tier-5 ML: routing (pure fn), churn scoring, recommendations, forecasting."""
from conftest import auth, login


async def _pids(client, h):
    prods = (await client.get("/api/v1/products", headers=h)).json()
    by_sku = {p["sku"]: p["id"] for p in prods}
    return by_sku["MILK-1L"], by_sku["GHEE-1KG"]


async def test_optimize_route_pure_fn(client):
    from app.services.ml import routing

    # Input order A -> C -> B is wasteful; optimal keeps A first then B then C.
    points = [
        {"id": 1, "lat": 0.0, "lng": 0.0},   # A
        {"id": 3, "lat": 0.0, "lng": 0.002},  # C (far)
        {"id": 2, "lat": 0.0, "lng": 0.001},  # B (middle)
    ]
    res = await routing.optimize_route(points)
    seq_ids = [s["id"] for s in res["ordered"]]
    assert seq_ids == [1, 2, 3]  # nearest-neighbour + 2-opt reorders B before C
    assert res["total_distance_km"] > 0
    assert [s["seq"] for s in res["ordered"]] == [1, 2, 3]


async def test_optimize_route_missing_coords_kept(client):
    from app.services.ml import routing

    res = await routing.optimize_route([
        {"id": 1, "lat": 0.0, "lng": 0.0},
        {"id": 2, "lat": None, "lng": None},  # no coords -> appended, input order kept
    ])
    ids = [s["id"] for s in res["ordered"]]
    assert set(ids) == {1, 2}


async def test_churn_scoring_and_listing(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    run = await client.post("/api/v1/admin/ml/churn/score", headers=ah)
    assert run.status_code == 200
    counts = run.json()
    assert counts["scored"] >= 1
    assert counts["scored"] == counts["low"] + counts["medium"] + counts["high"]

    listed = await client.get("/api/v1/admin/ml/churn", headers=ah)
    assert listed.status_code == 200
    rows = listed.json()
    assert rows and all(r["risk"] in ("low", "medium", "high") for r in rows)
    assert all(0.0 <= r["score"] <= 1.0 for r in rows)


async def test_churn_requires_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/ml/churn", headers=auth(cust))).status_code == 403
    assert (await client.post("/api/v1/admin/ml/churn/score", headers=auth(cust))).status_code == 403


async def test_co_purchase_recommendations(client):
    admin = await login(client, "9999900001")
    milk, ghee = await _pids(client, auth(admin))
    cust = await login(client, "9876543210")
    ch = auth(cust)
    # Two orders containing BOTH products -> they co-occur.
    for _ in range(2):
        r = await client.post(
            "/api/v1/orders",
            json={"items": [{"product_id": milk, "qty": 1}, {"product_id": ghee, "qty": 1}], "address": "x"},
            headers=ch,
        )
        assert r.status_code == 200
    recs = (await client.get(f"/api/v1/products/{milk}/recommendations")).json()
    assert ghee in [r["product_id"] for r in recs]


async def test_my_recommendations(client):
    admin = await login(client, "9999900001")
    milk, _ = await _pids(client, auth(admin))
    cust = await login(client, "9000000456")
    ch = auth(cust)
    await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 2}], "address": "x"}, headers=ch)
    recs = (await client.get("/api/v1/me/recommendations", headers=ch)).json()
    assert milk in [r["product_id"] for r in recs]


async def test_recommendations_unknown_product_404(client):
    r = await client.get("/api/v1/products/999999/recommendations")
    assert r.status_code == 404


async def test_forecast_still_works(client):
    admin = await login(client, "9999900001")
    r = await client.get("/api/v1/admin/forecast?date=2039-09-09", headers=auth(admin))
    assert r.status_code == 200
    assert "plan" in r.json()
