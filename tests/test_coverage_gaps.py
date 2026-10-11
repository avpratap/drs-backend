"""Coverage for endpoints the audit flagged as untested: payment methods, mandates,
products (cache), shops, invoices list, subscription detail, delete-address, consents."""
from conftest import auth, login


async def test_payment_methods_crud(client):
    u = await login(client, "9998887101")
    h = auth(u)
    p1 = await client.post("/api/v1/payment-methods", json={"method_type": "upi", "display": "a@ok"}, headers=h)
    assert p1.status_code == 201 and p1.json()["is_default"] is True
    p2 = await client.post(
        "/api/v1/payment-methods",
        json={"method_type": "card", "display": "4242", "is_default": True},
        headers=h,
    )
    assert p2.status_code == 201
    listing = (await client.get("/api/v1/payment-methods", headers=h)).json()
    assert sum(1 for m in listing if m["is_default"]) == 1
    # set default back to the first
    await client.post(f"/api/v1/payment-methods/{p1.json()['id']}/default", headers=h)
    listing = (await client.get("/api/v1/payment-methods", headers=h)).json()
    assert next(m for m in listing if m["is_default"])["id"] == p1.json()["id"]
    assert (await client.delete(f"/api/v1/payment-methods/{p1.json()['id']}", headers=h)).status_code == 204


async def test_mandates_crud(client):
    u = await login(client, "9998887102")
    h = auth(u)
    m = await client.post("/api/v1/mandates", json={"max_amount": 1500, "frequency": "monthly"}, headers=h)
    assert m.status_code == 201
    lst = await client.get("/api/v1/mandates", headers=h)
    assert lst.status_code == 200 and len(lst.json()) == 1


async def test_products_cache_and_create(client):
    admin = await login(client, "9999900001")
    a = await client.get("/api/v1/products")  # miss -> populates cache
    b = await client.get("/api/v1/products")  # hit
    assert a.status_code == b.status_code == 200
    n = len(a.json())
    created = await client.post(
        "/api/v1/products",
        json={"sku": "COV-GAP-1", "name": "Paneer 200g", "uom": "piece", "gst_rate": 5},
        headers=auth(admin),
    )
    assert created.status_code == 200
    after = await client.get("/api/v1/products")  # cache invalidated on create
    assert len(after.json()) == n + 1


async def test_shops_list_and_scoping(client):
    sub = await login(client, "9998887103")
    h = auth(sub)
    # A subscriber role is needed to create; promote via admin.
    admin = await login(client, "9999900001")
    uid = (await client.get("/api/v1/auth/me", headers=h)).json()["id"]
    await client.patch(f"/api/v1/admin/users/{uid}", json={"role": "subscriber"}, headers=auth(admin))
    h = auth(await login(client, "9998887103"))  # re-login after role change
    shop = await client.post("/api/v1/shops", json={"name": "Cov Shop"}, headers=h)
    sid = shop.json()["id"]
    assert (await client.get(f"/api/v1/shops/{sid}", headers=h)).status_code == 200
    mine = await client.get("/api/v1/shops", headers=h)
    assert any(s["id"] == sid for s in mine.json())
    # An unrelated customer can't see it (404, not 403 — no existence leak).
    other = await login(client, "9998887104")
    assert (await client.get(f"/api/v1/shops/{sid}", headers=auth(other))).status_code == 404


async def test_invoices_list(client):
    admin = await login(client, "9999900001")
    r = await client.get("/api/v1/invoices?limit=5", headers=auth(admin))
    assert r.status_code == 200 and isinstance(r.json(), list)


async def test_subscription_detail(client):
    admin = await login(client, "9999900001")
    subs = (await client.get("/api/v1/subscriptions", headers=auth(admin))).json()
    sid = subs[0]["id"]
    r = await client.get(f"/api/v1/subscriptions/{sid}", headers=auth(admin))
    assert r.status_code == 200 and "items" in r.json()


async def test_delete_address_and_consents(client):
    u = await login(client, "9998887105")
    h = auth(u)
    a = await client.post(
        "/api/v1/addresses",
        json={"line1": "9 Rd", "city": "Jaipur", "state": "Rajasthan", "pincode": "302001"},
        headers=h,
    )
    assert (await client.delete(f"/api/v1/addresses/{a.json()['id']}", headers=h)).status_code == 204
    assert (await client.get("/api/v1/addresses", headers=h)).json() == []
    # consents read: empty, then reflects a grant
    assert (await client.get("/api/v1/consents", headers=h)).json() == {}
    await client.post("/api/v1/consents", json={"purpose": "marketing_email", "action": "granted"}, headers=h)
    assert (await client.get("/api/v1/consents", headers=h)).json().get("marketing_email") == "granted"
