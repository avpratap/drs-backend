from conftest import auth, login

DATE = "2033-05-05"


async def test_wallet_low_emitted_on_delivery(client):
    """A prepaid account whose wallet drops below the threshold on delivery billing
    gets a wallet.low notice."""
    admin = await login(client, "9999900001")
    h = auth(admin)

    # Admin's own account is created prepaid_wallet by create_shop.
    shop = await client.post("/api/v1/shops", json={"name": "Prepaid Shop"}, headers=h)
    assert shop.status_code == 200
    shop_id = shop.json()["id"]

    sub = await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=h)
    sid = sub.json()["id"]
    await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": 1, "default_qty": 5, "frequency": "daily"}],
        headers=h,
    )

    await client.post(f"/api/v1/admin/cutoff/run?date={DATE}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={DATE}", headers=h)).json()
    mine = next(o for o in orders if o["shop_id"] == shop_id)
    item = mine["items"][0]

    deliver = await client.post(
        f"/api/v1/delivery/orders/{mine['id']}/deliver",
        json={
            "items": [{"product_id": item["product_id"], "qty_delivered": item["qty_planned"]}],
            "returns": [],
        },
        headers=h,
    )
    assert deliver.status_code == 200

    # Wallet is now negative (prepaid, debited on delivery) -> below threshold.
    bal = (await client.get("/api/v1/billing/me", headers=h)).json()["wallet_balance"]
    assert bal < 0

    notes = (await client.get("/api/v1/admin/notifications", headers=h)).json()
    low = [n for n in notes if n["event_type"] == "wallet.low"]
    assert low, "delivery dropping wallet below threshold should emit wallet.low"
