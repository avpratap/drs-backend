"""Tier-4 #38 delivery-app APIs: location, route, summary, failed-delivery."""
from conftest import auth, driver_today, login


async def _materialize(client, admin_h, date):
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=admin_h)


async def test_location_route_summary_fail(client, monkeypatch):
    date = "2039-08-08"
    driver_today(monkeypatch, date)
    admin = await login(client, "9999900001")
    ah = auth(admin)
    deliver = await login(client, "9999900003")
    dh = auth(deliver)
    await _materialize(client, ah, date)

    loc = await client.post("/api/v1/delivery/location", json={"lat": 26.9, "lng": 75.8}, headers=dh)
    assert loc.status_code == 200 and loc.json()["ok"] is True

    route = await client.get(f"/api/v1/delivery/orders/route?date={date}", headers=dh)
    assert route.status_code == 200
    stops = route.json()
    assert stops and stops[0]["sequence"] == 1
    oid = stops[0]["order"]["id"]
    failed0 = (await client.get(f"/api/v1/delivery/summary?date={date}", headers=dh)).json()["failed"]

    f = await client.post(
        f"/api/v1/delivery/orders/{oid}/fail", json={"reason": "customer_unavailable"}, headers=dh
    )
    assert f.status_code == 200
    assert f.json()["status"] == "failed" and f.json()["failure_reason"] == "customer_unavailable"

    s = await client.get(f"/api/v1/delivery/summary?date={date}", headers=dh)
    assert s.status_code == 200 and s.json()["failed"] == failed0 + 1

    # Terminal order can't be failed again.
    again = await client.post(
        f"/api/v1/delivery/orders/{oid}/fail", json={"reason": "refused"}, headers=dh
    )
    assert again.status_code == 409


async def test_fail_requires_assignment(client):
    from app.core.redis import redis_client

    date = "2039-08-09"
    admin = await login(client, "9999900001")
    ah = auth(admin)
    await _materialize(client, ah, date)
    oid = (await client.get(f"/api/v1/delivery/orders/route?date={date}", headers=ah)).json()[0]["order"]["id"]

    # Fresh user promoted to delivery but assigned to NO shop -> ownership 404.
    other = await login(client, "9000000777")
    uid = (await client.get("/api/v1/auth/me", headers=auth(other))).json()["id"]
    await client.patch(f"/api/v1/admin/users/{uid}", json={"role": "delivery"}, headers=ah)
    await redis_client.delete("otp:cooldown:9000000777")  # role change bumped token_version
    other2 = await login(client, "9000000777")
    r = await client.post(
        f"/api/v1/delivery/orders/{oid}/fail", json={"reason": "other"}, headers=auth(other2)
    )
    assert r.status_code == 404


async def test_location_requires_delivery_role(client):
    cust = await login(client, "9876543210")
    r = await client.post("/api/v1/delivery/location", json={"lat": 1, "lng": 1}, headers=auth(cust))
    assert r.status_code == 403
