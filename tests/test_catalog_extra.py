"""Storefront catalog extensions: sort, filters, categories, cart quote."""
from conftest import auth, login


async def _mk(client, h, sku, name=None, category=None):
    body = {"sku": sku, "name": name or sku}
    if category:
        body["category"] = category
    return (await client.post("/api/v1/products", json=body, headers=h)).json()["id"]


async def _ids_in_order(client, sort):
    rows = (await client.get(f"/api/v1/products?sort={sort}&limit=200")).json()
    return [p["id"] for p in rows]


async def test_sort_newest(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    a = await _mk(client, h, "SORT-NEW-A")
    b = await _mk(client, h, "SORT-NEW-B")  # created later -> higher id
    order = await _ids_in_order(client, "newest")
    assert order.index(b) < order.index(a)  # newest first


async def test_sort_name(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    a = await _mk(client, h, "SORTNAME-1", name="Zeta Sort Aaa")
    b = await _mk(client, h, "SORTNAME-2", name="Zeta Sort Bbb")
    order = await _ids_in_order(client, "name")
    assert order.index(a) < order.index(b)  # 'Aaa' before 'Bbb'


async def test_sort_rating_ok(client):
    r = await client.get("/api/v1/products?sort=rating&limit=50")
    assert r.status_code == 200 and isinstance(r.json(), list)


async def test_category_filter_and_list(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    pid = await _mk(client, h, "CAT-PROD-1", category="SpecialCat")
    # categories endpoint lists it
    cats = (await client.get("/api/v1/products/categories")).json()
    assert "SpecialCat" in cats
    # category filter returns only that product
    filtered = (await client.get("/api/v1/products?category=SpecialCat")).json()
    assert [p["id"] for p in filtered] == [pid]


async def test_cart_quote(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    ch = auth(cust)
    # Seeded GHEE-1KG: RETAIL price 500, gst 12%.
    prods = (await client.get("/api/v1/products", headers=ch)).json()
    ghee = next(p["id"] for p in prods if p["sku"] == "GHEE-1KG")
    # Start from a known-empty cart (other tests in the session may have left items).
    cart = (await client.get("/api/v1/cart", headers=ch)).json()
    for ln in cart["items"]:
        await client.delete(f"/api/v1/cart/items/{ln['id']}", headers=ch)
    await client.post("/api/v1/cart/items", json={"product_id": ghee, "qty": 1}, headers=ch)

    # A 10% coupon for the discount path.
    code = "QUOTE10"
    await client.post(
        "/api/v1/admin/coupons",
        json={"code": code, "discount_type": "percent", "value": 10, "min_order_value": 0},
        headers=ah,
    )
    q = await client.post(
        "/api/v1/cart/quote",
        json={"coupon_code": code, "pincode": "560001"},
        headers=ch,
    )
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["subtotal"] == 500.0
    assert body["discount"] == 50.0           # 10% of 500
    assert body["total"] == 450.0             # 500 - 50 (GST-inclusive payable)
    # Prices are GST-inclusive: tax is backed OUT of the discounted 450 → 450 - 450/1.12.
    assert body["tax_total"] == 48.21
    assert body["total_incl_tax"] == 450.0    # == total (GST already inside)
    assert body["serviceable"] is True        # dev-stub courier
    assert any(ln["product_id"] == ghee for ln in body["items"])
