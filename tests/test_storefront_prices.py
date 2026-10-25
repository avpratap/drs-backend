"""Public storefront prices: per-pack selling_price / product from_price come from the
cart's RETAIL resolver, so what the catalog shows equals what the cart charges."""
import uuid

from conftest import auth, login


async def _retail_tier(client, h) -> int:
    tiers = (await client.get("/api/v1/admin/price-tiers", headers=h)).json()
    return next(t["id"] for t in tiers if t["code"] == "RETAIL")


async def _setup(client, *, unit_price=80, min_qty=0.01, uom="litre", pack_size=1):
    """Fresh product with a RETAIL price; returns (admin headers, product id). min_qty
    defaults low so sub-1 packs resolve (the API default of 1 prices only packs >= 1)."""
    ah = auth(await login(client, "9999900001"))
    tag = uuid.uuid4().hex[:8]
    pid = (await client.post(
        "/api/v1/products",
        json={"sku": f"SP-{tag}", "name": f"Storefront {tag}", "uom": uom, "pack_size": pack_size},
        headers=ah,
    )).json()["id"]
    body = {"product_id": pid, "price_tier_id": await _retail_tier(client, ah),
            "unit_price": unit_price, "min_qty": min_qty, "valid_from": "2025-01-01"}
    r = await client.post("/api/v1/admin/product-prices", json=body, headers=ah)
    assert r.status_code == 200, r.text
    return ah, pid


async def _variant(client, ah, pid, label, pack, *, price=1, mrp=None, status_active=True):
    body = {"sku": f"{pid}-{label}", "label": label, "price": price, "pack_size": pack}
    if mrp is not None:
        body["mrp"] = mrp
    r = await client.post(f"/api/v1/products/{pid}/variants", json=body, headers=ah)
    assert r.status_code == 200, r.text
    vid = r.json()["id"]
    if not status_active:
        assert (await client.delete(f"/api/v1/products/variants/{vid}", headers=ah)).status_code == 204
    return vid


async def _cart_line_total(client, phone, pid, qty) -> float:
    h = auth(await login(client, phone))
    r = await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": qty}, headers=h)
    assert r.status_code == 200, r.text
    return next(i["line_total"] for i in r.json()["items"] if i["product_id"] == pid)


async def test_public_caller_sees_prices_equal_to_cart(client):
    ah, pid = await _setup(client, unit_price=80)
    await _variant(client, ah, pid, "500ml", 0.5, price=999, mrp=45)
    await _variant(client, ah, pid, "1L", 1, price=999, mrp=80)
    await _variant(client, ah, pid, "2L", 2, price=999, mrp=170)

    # Anonymous (no auth header) reads.
    variants = (await client.get(f"/api/v1/products/{pid}/variants")).json()
    full = (await client.get(f"/api/v1/products/{pid}/full")).json()
    assert full["variants"] == variants
    by_label = {v["label"]: v for v in variants}
    # unit_price x pack_size; the stale legacy `price` (999) is ignored.
    assert by_label["500ml"]["selling_price"] == 40.0
    assert by_label["1L"]["selling_price"] == 80.0
    assert by_label["2L"]["selling_price"] == 160.0
    assert all(v["price_available"] for v in variants)
    # Strike-through only when mrp > selling_price (1L: mrp == selling -> no strike).
    assert by_label["500ml"]["show_mrp_strike"] is True
    assert by_label["1L"]["show_mrp_strike"] is False
    assert by_label["2L"]["show_mrp_strike"] is True and by_label["2L"]["mrp"] == 170.0
    # "from" price = min active-variant selling price, on list, detail and /full.
    assert full["from_price"] == 40.0
    assert (await client.get(f"/api/v1/products/{pid}")).json()["from_price"] == 40.0
    listed = (await client.get("/api/v1/products?limit=200")).json()
    assert next(p for p in listed if p["id"] == pid)["from_price"] == 40.0

    # The exposed price is exactly what a B2C cart line computes for qty = pack_size.
    for i, v in enumerate(variants):
        assert await _cart_line_total(client, f"91000088{i:02d}", pid, v["pack_size"]) == v["selling_price"]


async def test_from_price_ignores_inactive_variants_and_falls_back_to_product_pack(client):
    ah, pid = await _setup(client, unit_price=50)
    await _variant(client, ah, pid, "250ml", 0.25, status_active=False)  # would be 12.5
    await _variant(client, ah, pid, "1L", 1)
    assert (await client.get(f"/api/v1/products/{pid}/full")).json()["from_price"] == 50.0

    # No variants at all: the product's own pack_size is the pack.
    _, solo = await _setup(client, unit_price=30, pack_size=2)
    assert (await client.get(f"/api/v1/products/{solo}")).json()["from_price"] == 60.0
    assert (await client.get(f"/api/v1/products/{solo}/full")).json()["from_price"] == 60.0
    assert await _cart_line_total(client, "9100008820", solo, 2) == 60.0


async def test_pack_below_retail_min_qty_is_price_unavailable_and_flagged(client):
    ah, pid = await _setup(client, unit_price=70, min_qty=1)
    half = await _variant(client, ah, pid, "500ml", 0.5)
    await _variant(client, ah, pid, "1L", 1)

    by_label = {v["label"]: v for v in (await client.get(f"/api/v1/products/{pid}/variants")).json()}
    assert by_label["500ml"]["selling_price"] is None
    assert by_label["500ml"]["price_available"] is False
    assert by_label["500ml"]["show_mrp_strike"] is False
    assert by_label["1L"]["selling_price"] == 70.0
    # The unpriced pack never drags the "from" price down to a guess.
    assert (await client.get(f"/api/v1/products/{pid}/full")).json()["from_price"] == 70.0

    flagged = (await client.get("/api/v1/admin/catalog/unpriced-packs", headers=ah)).json()
    mine = [f for f in flagged if f["product_id"] == pid]
    assert [(f["variant_id"], f["pack_size"]) for f in mine] == [(half, 0.5)]
    # Admin-only report.
    cust = auth(await login(client, "9100008821"))
    assert (await client.get("/api/v1/admin/catalog/unpriced-packs", headers=cust)).status_code == 403


async def test_list_price_is_fresh_even_with_warm_cache(client):
    from app.api.v1.products import _CACHE_KEY
    from app.core.cache import cache_get_json, cache_set_json

    ah, pid = await _setup(client, unit_price=20)
    await client.get("/api/v1/products")  # warms the default-page cache
    cached = await cache_get_json(_CACHE_KEY)
    assert cached is not None and all(p.get("from_price") is None for p in cached)  # prices never cached
    # Pin this product into the cached page (it may sit past the default page in the suite).
    own = (await client.get(f"/api/v1/products/{pid}")).json()
    await cache_set_json(_CACHE_KEY, [{**own, "from_price": None}])
    assert (await client.get("/api/v1/products")).json()[0]["from_price"] == 20.0

    await client.post("/api/v1/admin/product-prices", json={
        "product_id": pid, "price_tier_id": await _retail_tier(client, ah), "unit_price": 25,
        "min_qty": 0.01, "valid_from": "2025-06-01"}, headers=ah)
    # Even a cache entry written BEFORE the price change serves the new price.
    await cache_set_json(_CACHE_KEY, [{**own, "from_price": None}])
    assert (await client.get("/api/v1/products")).json()[0]["from_price"] == 25.0
    assert await _cart_line_total(client, "9100008822", pid, 1) == 25.0
