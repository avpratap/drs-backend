from conftest import auth, login


async def test_orders_pagination(client):
    cust = await login(client, "9876543210")
    for _ in range(3):
        await client.post(
            "/api/v1/orders", json={"items": [{"product_id": 1, "qty": 1}]}, headers=auth(cust)
        )
    one = await client.get("/api/v1/orders?limit=1", headers=auth(cust))
    assert one.status_code == 200
    assert len(one.json()) == 1

    two = await client.get("/api/v1/orders?limit=2&offset=1", headers=auth(cust))
    assert len(two.json()) == 2
    # offset moves the window: first id of page-2 differs from page-1.
    assert one.json()[0]["id"] != two.json()[0]["id"]


async def test_pagination_bounds_rejected(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/orders?limit=0", headers=auth(cust))).status_code == 422
    assert (await client.get("/api/v1/orders?limit=201", headers=auth(cust))).status_code == 422
    assert (await client.get("/api/v1/orders?offset=-1", headers=auth(cust))).status_code == 422


async def test_otp_per_ip_limit(client):
    # Distinct phones dodge the per-phone cap/cooldown, isolating the per-IP cap (50/hr).
    last = 200
    for i in range(51):
        r = await client.post("/api/v1/auth/otp/request", json={"phone": f"90000{i:05d}"})
        last = r.status_code
    assert last == 429  # 51st request from the same IP is throttled
