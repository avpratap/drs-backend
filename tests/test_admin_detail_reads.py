"""Single-resource admin reads the web admin's detail screens use (instead of
list-then-filter): GET /admin/products/{id} and GET /admin/locations/{id}."""
import uuid

from conftest import auth, login


async def test_admin_get_product_includes_retired(client):
    ah = auth(await login(client, "9999900001"))
    listed = (await client.get("/api/v1/admin/products?limit=200", headers=ah)).json()
    first = listed[0]
    r = await client.get(f"/api/v1/admin/products/{first['id']}", headers=ah)
    assert r.status_code == 200 and r.json() == first  # same shape as the list item

    sku = f"RET-{uuid.uuid4().hex[:8]}"
    pid = (await client.post("/api/v1/products", json={
        "sku": sku, "name": "Old Lassi", "uom": "litre", "pack_size": 1, "gst_rate": 0,
    }, headers=ah)).json()["id"]
    assert (await client.delete(f"/api/v1/products/{pid}", headers=ah)).status_code == 204
    r = await client.get(f"/api/v1/admin/products/{pid}", headers=ah)
    assert r.status_code == 200 and r.json()["status"] == "retired" and r.json()["sku"] == sku
    assert (await client.get(f"/api/v1/products/{pid}")).status_code == 404  # storefront hides it

    assert (await client.get("/api/v1/admin/products/999999999", headers=ah)).status_code == 404
    # The literal search route still wins over the id parameter.
    assert (await client.get("/api/v1/admin/products/search?q=milk", headers=ah)).status_code == 200


async def test_admin_get_location(client):
    ah = auth(await login(client, "9999900001"))
    name = f"Depot {uuid.uuid4().hex[:6]}"
    created = (await client.post("/api/v1/admin/locations", json={"name": name, "kind": "warehouse"},
                                 headers=ah)).json()
    r = await client.get(f"/api/v1/admin/locations/{created['id']}", headers=ah)
    assert r.status_code == 200 and r.json() == created
    assert (await client.get("/api/v1/admin/locations/999999999", headers=ah)).status_code == 404


async def test_admin_detail_reads_are_admin_only(client):
    cust = auth(await login(client, "9876543210"))
    for path in ("/api/v1/admin/products/1", "/api/v1/admin/locations/1"):
        assert (await client.get(path, headers=cust)).status_code == 403
        assert (await client.get(path)).status_code == 401
