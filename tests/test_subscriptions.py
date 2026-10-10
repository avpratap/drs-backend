from conftest import auth, login

FUTURE = "2031-03-03"  # distinct from the cutoff test's date
PAST = "2020-01-01"


async def _sub_id(client, token) -> int:
    subs = (await client.get("/api/v1/subscriptions", headers=auth(token))).json()
    return subs[0]["id"]


async def test_set_items_and_plan(client):
    admin = await login(client, "9999900001")
    sid = await _sub_id(client, admin)
    r = await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": 1, "default_qty": 4, "frequency": "daily"}],
        headers=auth(admin),
    )
    assert r.status_code == 200
    plan = (
        await client.get(f"/api/v1/subscriptions/{sid}/plan?date={FUTURE}", headers=auth(admin))
    ).json()
    line = next(line for line in plan["lines"] if line["product_id"] == 1)
    assert line["qty"] == 4


async def test_override_skip_removes_line(client):
    admin = await login(client, "9999900001")
    sid = await _sub_id(client, admin)
    await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": 1, "default_qty": 5, "frequency": "daily"}],
        headers=auth(admin),
    )
    r = await client.post(
        f"/api/v1/subscriptions/{sid}/overrides",
        json={"product_id": 1, "for_date": FUTURE, "override_qty": 0},
        headers=auth(admin),
    )
    assert r.status_code == 200
    plan = (
        await client.get(f"/api/v1/subscriptions/{sid}/plan?date={FUTURE}", headers=auth(admin))
    ).json()
    assert all(line["product_id"] != 1 for line in plan["lines"])


async def test_override_past_date_rejected(client):
    admin = await login(client, "9999900001")
    sid = await _sub_id(client, admin)
    r = await client.post(
        f"/api/v1/subscriptions/{sid}/overrides",
        json={"product_id": 1, "for_date": PAST, "override_qty": 2},
        headers=auth(admin),
    )
    assert r.status_code == 422  # can only edit tomorrow or later


async def test_subscription_scoping(client):
    # A plain customer owns no shops -> cannot see the subscriber's subscription.
    cust = await login(client, "9876543210")
    subs = (await client.get("/api/v1/subscriptions", headers=auth(cust))).json()
    assert subs == []
