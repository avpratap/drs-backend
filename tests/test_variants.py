from conftest import auth, login


async def _product(client, h, sku) -> int:
    return (await client.post("/api/v1/products", json={"sku": sku, "name": sku}, headers=h)).json()["id"]


async def _variant(client, h, pid, sku, label, price, sort_order=0):
    return await client.post(
        f"/api/v1/products/{pid}/variants",
        json={"sku": sku, "label": label, "price": price, "pack_size": 1, "sort_order": sort_order},
        headers=h,
    )


async def test_create_and_list_variants(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    pid = await _product(client, ah, "VAR-MILK")
    assert (await _variant(client, ah, pid, "VAR-MILK-1L", "1L", 60, sort_order=2)).status_code == 200
    assert (await _variant(client, ah, pid, "VAR-MILK-500", "500ml", 32, sort_order=1)).status_code == 200
    assert (await _variant(client, ah, pid, "VAR-MILK-2L", "2L", 115, sort_order=3)).status_code == 200

    # Public list: active, ordered by sort_order.
    pub = (await client.get(f"/api/v1/products/{pid}/variants")).json()
    assert [v["label"] for v in pub] == ["500ml", "1L", "2L"]

    # /full includes the product + its variants.
    full = (await client.get(f"/api/v1/products/{pid}/full")).json()
    assert full["id"] == pid and len(full["variants"]) == 3
    assert full["variants"][0]["label"] == "500ml"


async def test_update_and_soft_delete_variant(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    pid = await _product(client, ah, "VAR-GHEE")
    vid = (await _variant(client, ah, pid, "VAR-GHEE-1K", "1kg", 800)).json()["id"]

    upd = await client.patch(f"/api/v1/products/variants/{vid}", json={"price": 850, "label": "1 kg"}, headers=ah)
    assert upd.status_code == 200 and upd.json()["price"] == 850.0 and upd.json()["label"] == "1 kg"

    d = await client.delete(f"/api/v1/products/variants/{vid}", headers=ah)
    assert d.status_code == 204
    # Hidden from public list, but admin ?all=true still sees it.
    assert (await client.get(f"/api/v1/products/{pid}/variants")).json() == []
    allv = (await client.get(f"/api/v1/products/{pid}/variants?all=true", headers=ah)).json()
    assert len(allv) == 1 and allv[0]["status"] == "inactive"


async def test_duplicate_variant_sku_conflict(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    pid = await _product(client, ah, "VAR-DUP")
    assert (await _variant(client, ah, pid, "VAR-DUP-X", "X", 10)).status_code == 200
    assert (await _variant(client, ah, pid, "VAR-DUP-X", "Y", 20)).status_code == 409


async def test_variants_require_admin_and_known_product(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    pid = await _product(client, auth(admin), "VAR-GATE")
    # customer cannot create
    assert (await _variant(client, auth(cust), pid, "VAR-GATE-1", "1L", 50)).status_code == 403
    # unknown product
    assert (await _variant(client, auth(admin), 999999, "VAR-NOPE", "1L", 50)).status_code == 404
