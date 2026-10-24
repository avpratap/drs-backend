from conftest import auth, login


async def _fresh_sub(client, admin_h) -> int:
    """Own shop+subscription so status mutations don't touch the shared seeded one."""
    shop_id = (await client.post("/api/v1/shops", json={"name": "Status Shop"}, headers=admin_h)).json()["id"]
    return (await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=admin_h)).json()["id"]


async def test_subscription_pause_resume_cancel(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    sid = await _fresh_sub(client, h)

    assert (await client.post(f"/api/v1/subscriptions/{sid}/pause", headers=h)).json()["status"] == "paused"
    assert (await client.post(f"/api/v1/subscriptions/{sid}/resume", headers=h)).json()["status"] == "active"
    # Can't resume an active one.
    assert (await client.post(f"/api/v1/subscriptions/{sid}/resume", headers=h)).status_code == 200  # idempotent same-state
    cancelled = await client.post(f"/api/v1/subscriptions/{sid}/cancel", headers=h)
    assert cancelled.json()["status"] == "cancelled"
    # Cancelled is terminal -> cannot resume.
    assert (await client.post(f"/api/v1/subscriptions/{sid}/resume", headers=h)).status_code == 409


async def test_delivery_assignment_crud(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    # Make a shop to assign against.
    sub = await login(client, "9999900002")
    shop_id = (await client.post("/api/v1/shops", json={"name": "Assign Shop"}, headers=auth(sub))).json()["id"]
    # delivery user id
    deliver = await login(client, "9999900003")
    did = (await client.get("/api/v1/auth/me", headers=auth(deliver))).json()["id"]

    created = await client.post(
        "/api/v1/admin/delivery-assignments",
        json={"delivery_id": did, "shop_id": shop_id},
        headers=h,
    )
    assert created.status_code == 200 and created.json()["active"] is True
    aid = created.json()["id"]
    # Idempotent reactivate (same pair) returns same row.
    again = await client.post(
        "/api/v1/admin/delivery-assignments",
        json={"delivery_id": did, "shop_id": shop_id},
        headers=h,
    )
    assert again.json()["id"] == aid
    # Non-delivery user rejected.
    bad = await client.post(
        "/api/v1/admin/delivery-assignments",
        json={"delivery_id": 1, "shop_id": shop_id},
        headers=h,
    )
    assert bad.status_code == 422
    assert (await client.delete(f"/api/v1/admin/delivery-assignments/{aid}", headers=h)).status_code == 204
    assert (await client.post("/api/v1/admin/delivery-assignments", json={"delivery_id": did, "shop_id": shop_id}, headers=h)).status_code == 200


async def test_price_tier_and_price_crud(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    tier = await client.post(
        "/api/v1/admin/price-tiers",
        json={"code": "HORECA", "name": "Hotels/Restaurants", "tier_type": "b2b"},
        headers=h,
    )
    assert tier.status_code == 200
    tid = tier.json()["id"]
    # Duplicate code rejected.
    assert (await client.post("/api/v1/admin/price-tiers", json={"code": "HORECA", "name": "x"}, headers=h)).status_code == 409

    price = await client.post(
        "/api/v1/admin/product-prices",
        json={"product_id": 1, "price_tier_id": tid, "unit_price": 22.5, "min_qty": 10, "valid_from": "2025-01-01"},
        headers=h,
    )
    assert price.status_code == 200
    listed = await client.get("/api/v1/admin/product-prices?product_id=1", headers=h)
    assert listed.status_code == 200
    assert any(p["price_tier_id"] == tid and float(p["unit_price"]) == 22.5 and float(p["min_qty"]) == 10
               for p in listed.json())
    # bad validity window
    assert (await client.post("/api/v1/admin/product-prices", json={"product_id": 1, "price_tier_id": tid, "unit_price": 5, "valid_from": "2025-06-01", "valid_to": "2025-01-01"}, headers=h)).status_code == 422


async def test_tier_assignment_and_pricing_effect(client):
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    h = auth(admin)
    # New tier + a cheaper price for product 1.
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": "WHOLESALE", "name": "WS", "tier_type": "b2b"}, headers=h)).json()["id"]
    await client.post("/api/v1/admin/product-prices", json={"product_id": 1, "price_tier_id": tid, "unit_price": 20, "min_qty": 1, "valid_from": "2025-01-01"}, headers=h)
    shop_id = (await client.post("/api/v1/shops", json={"name": "Tier Shop"}, headers=auth(sub))).json()["id"]
    assert (await client.post(f"/api/v1/admin/shops/{shop_id}/tier", json={"price_tier_id": tid}, headers=h)).status_code == 200
    # The tier actually changes what the shop is charged: its standing order prices at 20.
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=auth(sub))).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": 1, "default_qty": 2, "frequency": "daily"}], headers=auth(sub))
    date = "2036-09-09"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=h)).json()
    mine = next(o for o in orders if o["shop_id"] == shop_id)
    assert mine["items"][0]["unit_price"] == 20.0 and mine["total"] == 40.0
    # bad tier
    assert (await client.post(f"/api/v1/admin/shops/{shop_id}/tier", json={"price_tier_id": 999999}, headers=h)).status_code == 404


async def test_management_requires_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/price-tiers", headers=auth(cust))).status_code == 403
