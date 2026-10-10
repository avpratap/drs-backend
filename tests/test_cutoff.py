from conftest import auth, login

DATE = "2030-06-01"  # far future: safely after any cutoff edit window


async def test_cutoff_generates_then_no_clobber(client):
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
    order = orders[0]
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
    run2 = await client.post(
        f"/api/v1/admin/cutoff/run?date={DATE}", json={}, headers=auth(admin)
    )
    assert run2.json()["orders_generated"] == 0
    after = (
        await client.get(f"/api/v1/delivery/orders?date={DATE}", headers=auth(delivery))
    ).json()
    assert after[0]["status"] == "delivered"
