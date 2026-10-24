"""Customer home aggregate + generic authenticated upload."""
from conftest import auth, login


async def test_upload_accepts_png_rejects_others(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buf, "PNG")  # a real (decodable) PNG
    ok = await client.post(
        "/api/v1/uploads", files={"file": ("a.png", buf.getvalue(), "image/png")}, headers=h
    )
    assert ok.status_code == 200 and ok.json()["url"]

    bad = await client.post(
        "/api/v1/uploads", files={"file": ("a.txt", b"hello", "text/plain")}, headers=h
    )
    assert bad.status_code == 422

    big = await client.post(
        "/api/v1/uploads",
        files={"file": ("big.png", b"x" * (5 * 1024 * 1024 + 1), "image/png")},
        headers=h,
    )
    assert big.status_code == 413


async def test_upload_requires_auth(client):
    r = await client.post("/api/v1/uploads", files={"file": ("a.png", b"\x89PNG", "image/png")})
    assert r.status_code == 401


async def test_home_aggregate(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    prods = (await client.get("/api/v1/products", headers=h)).json()
    pid = next(p["id"] for p in prods if p["sku"] == "MILK-1L")
    oid = (await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": 1}], "address": "12 MG Road"},
        headers=h,
    )).json()["id"]

    home = await client.get("/api/v1/me/home", headers=h)
    assert home.status_code == 200
    body = home.json()
    # Shape present.
    for key in ("wallet_balance", "credit_outstanding", "loyalty_points",
                "active_subscriptions", "recent_orders", "banners", "recommendations"):
        assert key in body
    # The just-placed order shows up in recent orders.
    assert any(o["id"] == oid for o in body["recent_orders"])
