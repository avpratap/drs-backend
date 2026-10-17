"""Tier-4 #39: wishlist, newsletter, reorder, tax-inclusive price."""
from conftest import auth, login


async def _pid(client, h, sku) -> int:
    prods = (await client.get("/api/v1/products", headers=h)).json()
    return next(p["id"] for p in prods if p["sku"] == sku)


async def test_wishlist_add_list_remove(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    pid = await _pid(client, h, "MILK-1L")
    # Idempotent add.
    await client.post("/api/v1/wishlist", json={"product_id": pid}, headers=h)
    r = await client.post("/api/v1/wishlist", json={"product_id": pid}, headers=h)
    assert r.status_code == 200
    assert sum(1 for w in r.json() if w["product_id"] == pid) == 1
    # Unknown product.
    assert (await client.post("/api/v1/wishlist", json={"product_id": 999999}, headers=h)).status_code == 404
    # Remove.
    assert (await client.delete(f"/api/v1/wishlist/{pid}", headers=h)).status_code == 204
    listing = (await client.get("/api/v1/wishlist", headers=h)).json()
    assert all(w["product_id"] != pid for w in listing)


async def test_wishlist_isolated_per_customer(client):
    a = await login(client, "9876543210")
    ha = auth(a)
    pid = await _pid(client, ha, "MILK-1L")
    await client.post("/api/v1/wishlist", json={"product_id": pid}, headers=ha)
    b = await login(client, "9000000456")
    hb = auth(b)
    assert (await client.get("/api/v1/wishlist", headers=hb)).json() == []


async def test_newsletter_subscribe_unsubscribe(client):
    # Double opt-in: subscribe is PENDING until confirmed (no forged consent).
    r1 = await client.post("/api/v1/newsletter/subscribe", json={"email": "fan@example.com", "name": "Fan"})
    assert r1.status_code == 200 and r1.json()["status"] == "pending"
    token = r1.json()["confirm_token"]  # returned in non-prod for testing
    assert token
    # GET is non-mutating (safe for email-scanner prefetch): still pending, no consent.
    status_get = await client.get(f"/api/v1/newsletter/confirm?token={token}")
    assert status_get.status_code == 200 and status_get.json()["status"] == "pending"
    # The explicit user action is a POST — that records consent.
    c = await client.post("/api/v1/newsletter/confirm", json={"token": token})
    assert c.status_code == 200 and c.json()["status"] == "subscribed"
    unsub_token = c.json()["unsubscribe_token"]  # returned in non-prod for testing
    assert unsub_token
    # A bad token is rejected on both the status GET and the confirm POST.
    assert (await client.get("/api/v1/newsletter/confirm?token=nope")).status_code == 404
    assert (await client.post("/api/v1/newsletter/confirm", json={"token": "nope-nope"})).status_code == 404
    # Unsubscribe needs the per-email token (can't unsubscribe someone else).
    bad = await client.post(
        "/api/v1/newsletter/unsubscribe", json={"email": "fan@example.com", "token": "0" * 32}
    )
    assert bad.status_code == 403
    u = await client.post(
        "/api/v1/newsletter/unsubscribe",
        json={"email": "fan@example.com", "token": unsub_token},
    )
    assert u.json()["status"] == "unsubscribed"
    # Admin list; admin-gate for customer.
    admin = await login(client, "9999900001")
    lst = await client.get("/api/v1/admin/newsletter/subscribers", headers=auth(admin))
    assert lst.status_code == 200 and any(s["email"] == "fan@example.com" for s in lst.json())
    cust = await login(client, "9876543210")
    assert (await client.get("/api/v1/admin/newsletter/subscribers", headers=auth(cust))).status_code == 403


async def test_reorder_clones_items(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    pid = await _pid(client, h, "MILK-1L")
    src = (await client.post("/api/v1/orders", json={"items": [{"product_id": pid, "qty": 3}]}, headers=h)).json()
    re = await client.post(f"/api/v1/orders/{src['id']}/reorder", headers=h)
    assert re.status_code == 200
    body = re.json()
    assert body["id"] != src["id"]
    assert any(i["product_id"] == pid and i["qty"] == 3 for i in body["items"])
    # Cross-customer reorder -> 404.
    other = await login(client, "9000000456")
    assert (await client.post(f"/api/v1/orders/{src['id']}/reorder", headers=auth(other))).status_code == 404


async def test_tax_inclusive_display(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    pid = await _pid(client, h, "GHEE-1KG")  # 12% GST, price 500
    order = (await client.post("/api/v1/orders", json={"items": [{"product_id": pid, "qty": 2}]}, headers=h)).json()
    item = next(i for i in order["items"] if i["product_id"] == pid)
    assert item["gst_rate"] == 12.0
    # Prices are GST-INCLUSIVE: charged 1000 -> taxable 1000/1.12=892.86, tax 107.14;
    # price_incl_tax == unit_price (500); total_incl_tax == total (GST already inside).
    assert item["gst_amount"] == 107.14
    assert item["price_incl_tax"] == 500.0
    assert order["tax_total"] == 107.14
    assert order["total_incl_tax"] == round(order["total"], 2)
