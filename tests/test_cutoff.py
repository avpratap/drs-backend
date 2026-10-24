from conftest import auth, driver_today, login

DATE = "2030-06-01"  # far future: safely after any cutoff edit window



async def _corner_store_id() -> int:
    import sqlalchemy as sa

    from app.core.db import engine

    async with engine.connect() as conn:
        return (await conn.execute(sa.text("SELECT id FROM shops WHERE name='Corner Store'"))).scalar()


async def test_cutoff_generates_then_no_clobber(client, monkeypatch):
    driver_today(monkeypatch, DATE)
    admin = await login(client, "9999900001")
    delivery = await login(client, "9999900003")

    run1 = await client.post(
        f"/api/v1/admin/cutoff/run?date={DATE}", json={}, headers=auth(admin)
    )
    assert run1.status_code == 200
    assert run1.json()["orders_generated"] >= 1

    orders = (
        await client.get(f"/api/v1/delivery/orders?date={DATE}", headers=auth(delivery))
    ).json()
    assert orders, "delivery user should see the generated order"
    # The seeded Corner Store order specifically (the driver may be assigned other shops
    # by other tests, so orders[0] could be anyone's).
    corner = await _corner_store_id()
    order = next(o for o in orders if o["shop_id"] == corner)
    item = order["items"][0]

    deliver = await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={
            "items": [{"product_id": item["product_id"], "qty_delivered": item["qty_planned"]}],
            "returns": [],
        },
        headers=auth(delivery),
    )
    assert deliver.status_code == 200
    assert deliver.json()["status"] == "delivered"

    # Re-running cutoff must NOT clobber the delivered order.
    # (Assert on THIS order, not global counts: other tests' subscriptions share the DB.)
    run2 = await client.post(
        f"/api/v1/admin/cutoff/run?date={DATE}", json={}, headers=auth(admin)
    )
    assert run2.status_code == 200
    after = (
        await client.get(f"/api/v1/delivery/orders?date={DATE}", headers=auth(delivery))
    ).json()
    mine = [o for o in after if o["shop_id"] == order["shop_id"]]
    assert len(mine) == 1 and mine[0]["id"] == order["id"]
    assert mine[0]["status"] == "delivered"
    assert mine[0]["items"][0]["qty_delivered"] == item["qty_planned"]


async def test_cutoff_emits_order_generated(client):
    admin = await login(client, "9999900001")
    date = "2032-02-02"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=auth(admin))
    # The seeded subscription owner (subscriber) should get an order.generated notice —
    # filtered to THAT user (other tests' runs also emit order.generated).
    import sqlalchemy as sa

    from app.core.db import engine

    async with engine.connect() as conn:
        owner = (await conn.execute(sa.text("SELECT id FROM users WHERE phone='9999900002'"))).scalar()
    notes = (
        await client.get(f"/api/v1/admin/notifications?user_id={owner}&limit=200", headers=auth(admin))
    ).json()
    gen = [n for n in notes if n["event_type"] == "order.generated"]
    assert gen, "cutoff should emit order.generated to the subscription owner"
    assert any(n["channel"] == "whatsapp" for n in gen)


async def test_concurrent_cutoff_runs_no_duplicate_orders(client):
    """T4: two concurrent cutoff runs for the same date must not double-materialize —
    UNIQUE(shop_id, delivery_date) + idempotency keep it to one delivery order per shop."""
    import asyncio

    admin = await login(client, "9999900001")
    delivery = await login(client, "9999900003")
    date = "2033-07-07"
    r1, r2 = await asyncio.gather(
        client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=auth(admin)),
        client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=auth(admin)),
        return_exceptions=True,
    )
    for r in (r1, r2):
        assert not isinstance(r, Exception) and r.status_code == 200, getattr(r, "text", r)
    # The seeded subscription has one shop -> exactly one delivery order for the date,
    # regardless of which run won the race.
    orders = (
        await client.get(f"/api/v1/delivery/orders?date={date}", headers=auth(delivery))
    ).json()
    shop_ids = [o["shop_id"] for o in orders]
    assert len(shop_ids) == len(set(shop_ids)), "no duplicate delivery order per shop"
    # Positive: the seeded shop's order exists (an empty list passed the check above).
    assert await _corner_store_id() in shop_ids
