from conftest import auth, login


async def test_create_with_category_then_filter_and_search(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    await client.post(
        "/api/v1/products",
        json={"sku": "CAT-GHEE", "name": "A2 Ghee", "category": "ghee", "gst_rate": 12},
        headers=h,
    )
    await client.post(
        "/api/v1/products",
        json={"sku": "CAT-CURD", "name": "Fresh Curd", "category": "curd", "gst_rate": 5},
        headers=h,
    )
    ghee = (await client.get("/api/v1/products?category=ghee")).json()
    assert ghee and all(p["category"] == "ghee" for p in ghee)
    found = (await client.get("/api/v1/products?q=curd")).json()
    assert any(p["sku"] == "CAT-CURD" for p in found)


async def test_update_and_retire_product(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = (
        await client.post(
            "/api/v1/products", json={"sku": "UPD-1", "name": "Old Name"}, headers=h
        )
    ).json()["id"]

    upd = await client.patch(
        f"/api/v1/products/{pid}",
        json={"name": "New Name", "image_url": "https://cdn/x.png", "category": "milk"},
        headers=h,
    )
    assert upd.status_code == 200
    assert upd.json()["name"] == "New Name" and upd.json()["category"] == "milk"

    assert (await client.get(f"/api/v1/products/{pid}")).status_code == 200
    assert (await client.delete(f"/api/v1/products/{pid}", headers=h)).status_code == 204
    # Retired product is hidden from detail + active list.
    assert (await client.get(f"/api/v1/products/{pid}")).status_code == 404
    listing = (await client.get("/api/v1/products")).json()
    assert all(p["id"] != pid for p in listing)


async def test_product_mutation_requires_admin(client):
    cust = await login(client, "9876543210")
    assert (await client.patch("/api/v1/products/1", json={"name": "x"}, headers=auth(cust))).status_code == 403
    assert (await client.delete("/api/v1/products/1", headers=auth(cust))).status_code == 403


async def test_get_missing_product_404(client):
    assert (await client.get("/api/v1/products/999999")).status_code == 404
