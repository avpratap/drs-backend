"""Admin dashboard support: X-Total-Count on admin lists, admin product list with a
status filter (retired products included), and GET of a single variant."""
import pytest
from conftest import auth, login


async def _product(client, h, sku, **extra) -> int:
    r = await client.post("/api/v1/products", json={"sku": sku, "name": sku, **extra}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_admin_product_list_status_filter_includes_retired(client):
    ah = auth(await login(client, "9999900001"))
    live = await _product(client, ah, "ALP-LIVE-1", category="alp-cat")
    gone = await _product(client, ah, "ALP-GONE-1", category="alp-cat")
    assert (await client.delete(f"/api/v1/products/{gone}", headers=ah)).status_code == 204

    # Storefront list never shows retired products...
    pub = (await client.get("/api/v1/products", params={"q": "ALP-", "limit": 200})).json()
    assert {p["id"] for p in pub} == {live}

    # ...the admin list does, and filters by status on request.
    every = await client.get("/api/v1/admin/products", params={"q": "ALP-"}, headers=ah)
    assert every.status_code == 200
    assert {p["id"] for p in every.json()} == {live, gone}
    assert every.headers["x-total-count"] == "2"

    retired = (
        await client.get("/api/v1/admin/products", params={"q": "ALP-", "status": "retired"}, headers=ah)
    ).json()
    assert [p["id"] for p in retired] == [gone] and retired[0]["status"] == "retired"
    active = (
        await client.get("/api/v1/admin/products", params={"category": "alp-cat", "status": "active"}, headers=ah)
    ).json()
    assert [p["id"] for p in active] == [live]


async def test_admin_product_list_rejects_unknown_status_and_non_admins(client):
    ah = auth(await login(client, "9999900001"))
    bad = await client.get("/api/v1/admin/products", params={"status": "bogus"}, headers=ah)
    assert bad.status_code == 422
    ch = auth(await login(client, "9876543210"))
    assert (await client.get("/api/v1/admin/products", headers=ch)).status_code == 403
    assert (await client.get("/api/v1/admin/products")).status_code in (401, 403)


async def test_admin_product_list_search_escapes_like_metacharacters(client):
    ah = auth(await login(client, "9999900001"))
    await _product(client, ah, "ALP-ESC-1")
    r = await client.get("/api/v1/admin/products", params={"q": "%"}, headers=ah)
    assert r.status_code == 200 and r.json() == [] and r.headers["x-total-count"] == "0"


async def test_total_count_is_unpaginated_count(client):
    ah = auth(await login(client, "9999900001"))
    for i in range(5):
        await _product(client, ah, f"ALP-PAGE-{i}")
    r = await client.get("/api/v1/admin/products", params={"q": "ALP-PAGE-", "limit": 2, "offset": 2}, headers=ah)
    assert r.status_code == 200
    assert len(r.json()) == 2
    assert r.headers["x-total-count"] == "5"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/payments",
        "/api/v1/admin/refunds",
        "/api/v1/admin/accounts",
        "/api/v1/admin/orders",
        "/api/v1/admin/audit-log",
        "/api/v1/admin/outbound-webhooks",
        "/api/v1/admin/products/search?q=milk",
        "/api/v1/admin/content/articles",
        "/api/v1/admin/content/pages",
        "/api/v1/admin/content/banners",
        "/api/v1/admin/locations",
        "/api/v1/admin/stock",
        "/api/v1/admin/stock/low",
        "/api/v1/admin/stock/reorder-suggestions",
        "/api/v1/admin/stock/batches",
        "/api/v1/admin/stock/1/movements",
        "/api/v1/admin/loyalty",
        "/api/v1/admin/gift-cards",
        "/api/v1/admin/delivery-assignments",
        "/api/v1/admin/product-prices",
        "/api/v1/admin/users",
        "/api/v1/admin/coupons",
        "/api/v1/admin/shops",
        "/api/v1/admin/withdrawals",
        "/api/v1/admin/ml/churn",
        "/api/v1/admin/newsletter/subscribers",
        "/api/v1/admin/suppliers",
        "/api/v1/admin/purchase-orders",
        "/api/v1/admin/boms",
        "/api/v1/admin/production-orders",
        "/api/v1/admin/ratings",
        "/api/v1/admin/returns",
        "/api/v1/admin/support/tickets",
        "/api/v1/admin/transfers",
        "/api/v1/admin/products",
    ],
)
async def test_admin_lists_report_total_count(client, path):
    ah = auth(await login(client, "9999900001"))
    r = await client.get(path, headers=ah)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    total = int(r.headers["x-total-count"])
    assert total >= len(body)
    # A one-row page still reports the full count.
    sep = "&" if "?" in path else "?"
    one = await client.get(f"{path}{sep}limit=1", headers=ah)
    assert one.headers["x-total-count"] == str(total)
    assert len(one.json()) == min(1, total)


async def test_total_count_header_is_cors_exposed(client):
    r = await client.options(
        "/api/v1/admin/products",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 200
    ah = auth(await login(client, "9999900001"))
    g = await client.get("/api/v1/admin/products", headers={**ah, "Origin": "http://localhost:3000"})
    assert "x-total-count" in g.headers.get("access-control-expose-headers", "").lower()


async def test_get_single_variant(client):
    ah = auth(await login(client, "9999900001"))
    pid = await _product(client, ah, "ALP-VAR-P")
    v = await client.post(
        f"/api/v1/products/{pid}/variants",
        json={"sku": "ALP-VAR-500", "label": "500 ml", "price": 32, "pack_size": 0.5},
        headers=ah,
    )
    vid = v.json()["id"]

    got = await client.get(f"/api/v1/products/variants/{vid}", headers=ah)
    assert got.status_code == 200
    assert got.json() == v.json()

    # Inactive (retired) variants are still readable by admins.
    assert (await client.delete(f"/api/v1/products/variants/{vid}", headers=ah)).status_code == 204
    again = await client.get(f"/api/v1/products/variants/{vid}", headers=ah)
    assert again.status_code == 200 and again.json()["status"] == "inactive"

    assert (await client.get("/api/v1/products/variants/999999", headers=ah)).status_code == 404


async def test_get_single_variant_requires_admin(client):
    ah = auth(await login(client, "9999900001"))
    pid = await _product(client, ah, "ALP-VAR-AUTH")
    vid = (
        await client.post(
            f"/api/v1/products/{pid}/variants", json={"sku": "ALP-VAR-AUTH-1", "label": "1"}, headers=ah
        )
    ).json()["id"]
    ch = auth(await login(client, "9876543210"))
    assert (await client.get(f"/api/v1/products/variants/{vid}", headers=ch)).status_code == 403
