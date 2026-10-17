"""Admin CRUD completion: pricing/coupon edit+delete, CMS page/banner, loyalty admin."""
from conftest import auth, login


async def _admin(client):
    return auth(await login(client, "9999900001"))


async def test_price_tier_edit_get_delete(client):
    h = await _admin(client)
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": "CRUDT1", "name": "Crud Tier"}, headers=h)).json()["id"]
    assert (await client.get(f"/api/v1/admin/price-tiers/{tid}", headers=h)).json()["code"] == "CRUDT1"
    p = await client.patch(f"/api/v1/admin/price-tiers/{tid}", json={"name": "Renamed"}, headers=h)
    assert p.status_code == 200 and p.json()["name"] == "Renamed"
    # In-use tier can't be deleted.
    pid = (await client.post("/api/v1/products", json={"sku": "CRUD-PR-1", "name": "p"}, headers=h)).json()["id"]
    price = (await client.post("/api/v1/admin/product-prices", json={"product_id": pid, "price_tier_id": tid, "unit_price": 10, "valid_from": "2025-01-01"}, headers=h)).json()["id"]
    assert (await client.delete(f"/api/v1/admin/price-tiers/{tid}", headers=h)).status_code == 409
    # Remove the price, then the tier deletes.
    assert (await client.delete(f"/api/v1/admin/product-prices/{price}", headers=h)).status_code == 204
    assert (await client.delete(f"/api/v1/admin/price-tiers/{tid}", headers=h)).status_code == 204


async def test_product_price_edit_get(client):
    h = await _admin(client)
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": "CRUDT2", "name": "t"}, headers=h)).json()["id"]
    pid = (await client.post("/api/v1/products", json={"sku": "CRUD-PR-2", "name": "p"}, headers=h)).json()["id"]
    price = (await client.post("/api/v1/admin/product-prices", json={"product_id": pid, "price_tier_id": tid, "unit_price": 10, "valid_from": "2025-01-01"}, headers=h)).json()["id"]
    assert (await client.get(f"/api/v1/admin/product-prices/{price}", headers=h)).json()["unit_price"] == 10.0
    up = await client.patch(f"/api/v1/admin/product-prices/{price}", json={"unit_price": 22.5}, headers=h)
    assert up.status_code == 200 and up.json()["unit_price"] == 22.5


async def test_coupon_edit_get(client):
    h = await _admin(client)
    cid = (await client.post("/api/v1/admin/coupons", json={"code": "CRUDCOUP", "discount_type": "flat", "value": 50}, headers=h)).json()["id"]
    assert (await client.get(f"/api/v1/admin/coupons/{cid}", headers=h)).json()["value"] == 50.0
    e = await client.patch(f"/api/v1/admin/coupons/{cid}", json={"value": 75, "active": False}, headers=h)
    assert e.status_code == 200 and e.json()["value"] == 75.0 and e.json()["active"] is False


async def test_cms_page_unpublish_delete_and_banner_edit(client):
    h = await _admin(client)
    page = (await client.post("/api/v1/admin/content/pages", json={"slug": "crud-page", "title": "T", "body_html": "<p>x</p>"}, headers=h)).json()
    pub = await client.post(f"/api/v1/admin/content/pages/{page['id']}/publish", headers=h)
    assert pub.json()["status"] == "published"
    unp = await client.post(f"/api/v1/admin/content/pages/{page['id']}/unpublish", headers=h)
    assert unp.json()["status"] == "draft"
    assert (await client.delete(f"/api/v1/admin/content/pages/{page['id']}", headers=h)).status_code == 204

    banner = (await client.post("/api/v1/admin/content/banners", json={"title": "B", "image_url": "http://x/i.jpg"}, headers=h)).json()
    ed = await client.patch(f"/api/v1/admin/content/banners/{banner['id']}", json={"title": "B2", "active": False}, headers=h)
    assert ed.status_code == 200 and ed.json()["title"] == "B2" and ed.json()["active"] is False


async def test_loyalty_and_gift_card_admin(client):
    h = await _admin(client)
    cust = await login(client, "9876543210")
    await client.get("/api/v1/loyalty", headers=auth(cust))  # creates the loyalty account
    lst = await client.get("/api/v1/admin/loyalty", headers=h)
    assert lst.status_code == 200 and isinstance(lst.json(), list)

    gc = (await client.post("/api/v1/admin/gift-cards", json={"amount": 500}, headers=h)).json()
    gid = gc["id"] if "id" in gc else None
    listed = await client.get("/api/v1/admin/gift-cards", headers=h)
    assert listed.status_code == 200
    gid = next(g["id"] for g in listed.json() if g["code"] == gc["code"])
    v = await client.post(f"/api/v1/admin/gift-cards/{gid}/void", headers=h)
    assert v.status_code == 200 and v.json()["status"] == "disabled"
    assert (await client.post(f"/api/v1/admin/gift-cards/{gid}/void", headers=h)).status_code == 409


async def test_admin_crud_requires_admin(client):
    hc = auth(await login(client, "9876543210"))
    assert (await client.patch("/api/v1/admin/coupons/1", json={"value": 5}, headers=hc)).status_code == 403
    assert (await client.get("/api/v1/admin/loyalty", headers=hc)).status_code == 403
    assert (await client.patch("/api/v1/admin/content/banners/1", json={"title": "x"}, headers=hc)).status_code == 403
    assert (await client.delete("/api/v1/admin/price-tiers/1", headers=hc)).status_code == 403
