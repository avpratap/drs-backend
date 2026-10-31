from conftest import auth, login

FUTURE = "2031-03-03"  # distinct from the cutoff test's date
PAST = "2020-01-01"


async def _sub_id(client, token) -> int:
    subs = (await client.get("/api/v1/subscriptions", headers=auth(token))).json()
    return subs[0]["id"]


async def _fresh_sub(client, token, name) -> int:
    """A subscription on its OWN shop, so writes here never leak into the seeded
    subscription that other test files (cutoff, forecasting, billing) depend on."""
    shop = (await client.post("/api/v1/shops", json={"name": name}, headers=auth(token))).json()["id"]
    return (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=auth(token))).json()["id"]


async def test_set_items_and_plan(client):
    admin = await login(client, "9999900001")
    sid = await _fresh_sub(client, admin, "Sub items shop")
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
    sid = await _fresh_sub(client, admin, "Sub override shop")
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
    # A fresh customer owns no shops -> cannot see another account's subscription.
    # (A retail customer only ever sees subscriptions under their own home shop.)
    cust = await login(client, "9100000555")
    subs = (await client.get("/api/v1/subscriptions", headers=auth(cust))).json()
    assert subs == []


async def _plan_qty(client, admin, sid, product_id=1, date=FUTURE):
    plan = (await client.get(f"/api/v1/subscriptions/{sid}/plan?date={date}", headers=auth(admin))).json()
    return next((ln["qty"] for ln in plan["lines"] if ln["product_id"] == product_id), None)


async def test_dropped_item_overrides_do_not_resurrect(client):
    """B2: dropping a product and re-adding it later must not bring back its old future
    override (re-adding reactivates the same item row, and an override forces its date)."""
    import datetime as dt

    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models.subscription import SubscriptionItem, SubscriptionOverride

    admin = await login(client, "9999900001")
    sid = await _fresh_sub(client, admin, "Sub resurrect shop")
    url = f"/api/v1/subscriptions/{sid}/items"
    await client.put(url, json=[{"product_id": 1, "default_qty": 2, "frequency": "daily"}],
                     headers=auth(admin))
    r = await client.post(f"/api/v1/subscriptions/{sid}/overrides",
                          json={"product_id": 1, "for_date": FUTURE, "override_qty": 9},
                          headers=auth(admin))
    assert r.status_code == 200 and await _plan_qty(client, admin, sid) == 9
    async with SessionLocal() as db:  # a past override: history, must survive
        item_id = await db.scalar(select(SubscriptionItem.id).where(
            SubscriptionItem.subscription_id == sid, SubscriptionItem.product_id == 1))
        db.add(SubscriptionOverride(subscription_item_id=item_id, for_date=dt.date(2020, 1, 1),
                                    override_qty=3))
        await db.commit()

    # Drop product 1, then re-add it with a new standing qty.
    await client.put(url, json=[{"product_id": 2, "default_qty": 1, "frequency": "daily"}],
                     headers=auth(admin))
    assert await _plan_qty(client, admin, sid) is None
    r = await client.put(url, json=[{"product_id": 1, "default_qty": 4, "frequency": "daily"}],
                         headers=auth(admin))
    assert r.status_code == 200
    assert await _plan_qty(client, admin, sid) == 4  # current intent, not the stale 9

    async with SessionLocal() as db:
        dates = set((await db.execute(select(SubscriptionOverride.for_date).where(
            SubscriptionOverride.subscription_item_id == item_id))).scalars())
    assert dates == {dt.date(2020, 1, 1)}


async def test_readding_clears_orphans_left_before_the_fix(client):
    """Rows dropped before this fix still hold future overrides: re-adding clears them."""
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models.subscription import SubscriptionItem

    admin = await login(client, "9999900001")
    sid = await _fresh_sub(client, admin, "Sub orphan shop")
    url = f"/api/v1/subscriptions/{sid}/items"
    await client.put(url, json=[{"product_id": 1, "default_qty": 2, "frequency": "daily"}],
                     headers=auth(admin))
    await client.post(f"/api/v1/subscriptions/{sid}/overrides",
                      json={"product_id": 1, "for_date": FUTURE, "override_qty": 7},
                      headers=auth(admin))
    async with SessionLocal() as db:  # legacy drop: deactivated, override left behind
        item = await db.scalar(select(SubscriptionItem).where(
            SubscriptionItem.subscription_id == sid, SubscriptionItem.product_id == 1))
        item.active = False
        await db.commit()
    await client.put(url, json=[{"product_id": 1, "default_qty": 3, "frequency": "daily"}],
                     headers=auth(admin))
    assert await _plan_qty(client, admin, sid) == 3
