from conftest import auth, login

# 1x1 transparent PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


async def _delivered_invoice(client, admin_h):
    shop_id = (await client.post("/api/v1/shops", json={"name": "PDF Shop", "state": "Maharashtra"}, headers=admin_h)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=admin_h)).json()["id"]
    await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": 2, "default_qty": 2, "frequency": "daily"}],
        headers=admin_h,
    )
    date = "2037-07-07"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=admin_h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=admin_h)).json()
    order = next(o for o in orders if o["shop_id"] == shop_id)
    item = order["items"][0]
    await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={"items": [{"product_id": item["product_id"], "qty_delivered": item["qty_planned"]}], "returns": []},
        headers=admin_h,
    )
    return (await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={order['id']}", headers=admin_h)).json()["id"]


async def test_invoice_pdf_is_private_signed_and_cached(client):
    """G2: invoice PDFs (GSTIN, address, purchases) are never at a public, guessable URL.
    The endpoint mints a short-lived signed link; the bare key is not servable."""
    import sqlalchemy as sa

    from app.core.db import engine

    admin = await login(client, "9999900001")
    h = auth(admin)
    inv_id = await _delivered_invoice(client, h)
    r1 = await client.get(f"/api/v1/invoices/{inv_id}/pdf", headers=h)
    assert r1.status_code == 200
    url = r1.json()["pdf_url"]
    path, _, query = url.partition("?")
    assert path.startswith("/files/private/invoices/") and path.endswith(".pdf")
    assert "sig=" in query and "exp=" in query
    served = await client.get(url)
    assert served.status_code == 200 and served.content.startswith(b"%PDF")
    # Without (or with a tampered) signature the object is not reachable.
    assert (await client.get(path)).status_code == 404
    assert (await client.get(url[:-4] + "0000")).status_code == 404
    # Stored once: the second request re-signs the SAME private key (no re-render).
    async with engine.connect() as conn:
        key = (await conn.execute(sa.text("SELECT pdf_url FROM invoices WHERE id=:i"), {"i": inv_id})).scalar()
    assert key.startswith("private/invoices/") and len(key.rsplit("-", 1)[-1]) == len("0" * 32 + ".pdf")
    r2 = await client.get(f"/api/v1/invoices/{inv_id}/pdf", headers=h)
    assert r2.json()["pdf_url"].partition("?")[0] == path
    # Another customer can't mint a link for it.
    other = await login(client, "9191919191")
    assert (await client.get(f"/api/v1/invoices/{inv_id}/pdf", headers=auth(other))).status_code == 404


async def test_pod_upload(client):
    delivery = await login(client, "9999900003")
    r = await client.post(
        "/api/v1/delivery/uploads",
        files={"file": ("pod.png", _PNG, "image/png")},
        headers=auth(delivery),
    )
    assert r.status_code == 200 and r.json()["url"].endswith(".png")


async def test_pod_upload_rejects_bad_type_and_role(client):
    delivery = await login(client, "9999900003")
    bad = await client.post(
        "/api/v1/delivery/uploads",
        files={"file": ("x.txt", b"hello", "text/plain")},
        headers=auth(delivery),
    )
    assert bad.status_code == 422
    cust = await login(client, "9876543210")
    denied = await client.post(
        "/api/v1/delivery/uploads",
        files={"file": ("pod.png", _PNG, "image/png")},
        headers=auth(cust),
    )
    assert denied.status_code == 403
