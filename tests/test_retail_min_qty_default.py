"""A retail-tier price created without min_qty starts at the product's smallest pack (not
1), so sub-1 packs stay priceable. Explicit min_qty, B2B tiers and PATCH are unchanged."""
import uuid

from conftest import auth, login


async def _admin(client):
    return auth(await login(client, "9999900001"))


async def _retail(client, h) -> int:
    tiers = (await client.get("/api/v1/admin/price-tiers", headers=h)).json()
    return next(t["id"] for t in tiers if t["code"] == "RETAIL")


async def _product(client, h, *, pack_size=1, packs=()):
    tag = uuid.uuid4().hex[:8]
    pid = (await client.post("/api/v1/products", json={
        "sku": f"MQ-{tag}", "name": f"MQ {tag}", "uom": "litre", "pack_size": pack_size,
    }, headers=h)).json()["id"]
    for i, (pack, active) in enumerate(packs):
        r = await client.post(f"/api/v1/products/{pid}/variants", json={
            "sku": f"MQ-{tag}-{i}", "label": f"{pack}L", "price": 1, "pack_size": pack,
        }, headers=h)
        if not active:
            await client.delete(f"/api/v1/products/variants/{r.json()['id']}", headers=h)
    return pid


async def _price(client, h, pid, tid, **extra):
    r = await client.post("/api/v1/admin/product-prices", json={
        "product_id": pid, "price_tier_id": tid, "unit_price": 60, "valid_from": "2025-01-01",
        **extra}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_retail_price_without_min_qty_starts_at_smallest_pack(client):
    h = await _admin(client)
    # 0.25 is inactive: only ACTIVE variants count.
    pid = await _product(client, h, packs=[(1, True), (0.5, True), (0.25, False)])
    row = await _price(client, h, pid, await _retail(client, h))
    assert row["min_qty"] == 0.5

    by_label = {v["label"]: v for v in (await client.get(f"/api/v1/products/{pid}/variants")).json()}
    assert by_label["0.5L"]["price_available"] is True and by_label["0.5L"]["selling_price"] == 30.0
    assert by_label["1L"]["selling_price"] == 60.0
    unpriced = (await client.get("/api/v1/admin/catalog/unpriced-packs", headers=h)).json()
    assert not [u for u in unpriced if u["product_id"] == pid]
    # ...and the cart charges it at qty = pack_size.
    ch = auth(await login(client, "9100009911"))
    r = await client.post("/api/v1/cart/items", json={"product_id": pid, "qty": 0.5}, headers=ch)
    assert r.status_code == 200, r.text
    assert next(i for i in r.json()["items"] if i["product_id"] == pid)["line_total"] == 30.0


async def test_default_falls_back_to_product_pack_and_rounds_down(client):
    h = await _admin(client)
    tid = await _retail(client, h)
    assert (await _price(client, h, await _product(client, h, pack_size=0.2), tid))["min_qty"] == 0.2
    # min_qty is 2 dp: a 0.125 pack must round DOWN (0.13 would leave it unpriced).
    odd = await _product(client, h, packs=[(0.125, True)])
    assert (await _price(client, h, odd, tid))["min_qty"] == 0.12
    v = (await client.get(f"/api/v1/products/{odd}/variants")).json()[0]
    assert v["selling_price"] == round(60 * 0.125, 2)


async def test_explicit_min_qty_b2b_and_patch_unchanged(client):
    h = await _admin(client)
    pid = await _product(client, h, packs=[(0.5, True)])
    retail = await _retail(client, h)
    # Explicit min_qty is honoured as given (a volume break on the retail tier).
    assert (await _price(client, h, pid, retail, min_qty=10))["min_qty"] == 10.0
    # A B2B tier keeps the historical default of 1.
    b2b = (await client.post("/api/v1/admin/price-tiers", json={
        "code": f"B2B{uuid.uuid4().hex[:6]}", "name": "b2b", "tier_type": "b2b"}, headers=h)).json()
    row = await _price(client, h, pid, b2b["id"])
    assert row["min_qty"] == 1.0
    # PATCH without min_qty leaves the existing row's min_qty alone.
    r = await client.patch(f"/api/v1/admin/product-prices/{row['id']}", json={"unit_price": 55}, headers=h)
    assert r.status_code == 200 and r.json()["min_qty"] == 1.0 and r.json()["unit_price"] == 55.0


async def test_retail_default_unknown_product_404(client):
    h = await _admin(client)
    r = await client.post("/api/v1/admin/product-prices", json={
        "product_id": 99999999, "price_tier_id": await _retail(client, h), "unit_price": 5,
        "valid_from": "2025-01-01"}, headers=h)
    assert r.status_code == 404
