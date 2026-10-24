from conftest import auth, login

DATE = "2034-07-07"


async def _delivered_ghee_order(client, admin_h, state=None):
    """Admin sets up a shop+subscription for the 12% ghee product, cuts off, delivers."""
    payload = {"name": "Invoice Shop"}
    if state is not None:
        payload["state"] = state
    shop = await client.post("/api/v1/shops", json=payload, headers=admin_h)
    shop_id = shop.json()["id"]
    sub = await client.post("/api/v1/subscriptions", json={"shop_id": shop_id}, headers=admin_h)
    sid = sub.json()["id"]
    await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": 2, "default_qty": 2, "frequency": "daily"}],  # ghee
        headers=admin_h,
    )
    await client.post(f"/api/v1/admin/cutoff/run?date={DATE}", json={}, headers=admin_h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={DATE}", headers=admin_h)).json()
    order = next(o for o in orders if o["shop_id"] == shop_id)
    item = order["items"][0]
    await client.post(
        f"/api/v1/delivery/orders/{order['id']}/deliver",
        json={
            "items": [{"product_id": item["product_id"], "qty_delivered": item["qty_planned"]}],
            "returns": [],
        },
        headers=admin_h,
    )
    return order["id"]


async def test_generate_invoice_gst_split(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    do_id = await _delivered_ghee_order(client, h)

    r = await client.post(
        f"/api/v1/admin/invoices/generate?delivery_order_id={do_id}", headers=h
    )
    assert r.status_code == 200
    inv = r.json()
    # Prices are GST-INCLUSIVE: ghee 2 x 500 = 1000 charged @ 12% intra-state ->
    # taxable 1000/1.12 = 892.86, tax 107.14 split CGST 53.57 + SGST 53.57, total 1000.
    assert inv["taxable_value"] == 892.86
    assert inv["cgst"] == 53.57
    assert inv["sgst"] == 53.57
    assert inv["igst"] == 0.0
    assert inv["total"] == 1000.0
    assert inv["is_interstate"] is False
    assert inv["invoice_no"].startswith("DRS/")
    assert inv["lines"][0]["hsn_code"] == "04059020"


async def test_interstate_invoice_uses_igst(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    # Shop in a different state than the seller (Rajasthan) -> IGST, no CGST/SGST.
    do_id = await _delivered_ghee_order(client, h, state="Maharashtra")
    inv = (
        await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={do_id}", headers=h)
    ).json()
    assert inv["is_interstate"] is True
    assert inv["igst"] == 107.14  # GST-inclusive: 1000 - 1000/1.12
    assert inv["cgst"] == 0.0 and inv["sgst"] == 0.0
    assert inv["taxable_value"] == 892.86
    assert inv["total"] == 1000.0


async def test_invoice_idempotent(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    do_id = await _delivered_ghee_order(client, h)
    a = await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={do_id}", headers=h)
    b = await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={do_id}", headers=h)
    assert a.json()["id"] == b.json()["id"]  # one invoice per delivery order


async def test_credit_note_negates(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    do_id = await _delivered_ghee_order(client, h)
    inv = (
        await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={do_id}", headers=h)
    ).json()
    cn = await client.post(
        f"/api/v1/admin/invoices/{inv['id']}/credit-note",
        json={"reason": "returned"},
        headers=h,
    )
    assert cn.status_code == 200
    note = cn.json()
    assert note["is_credit_note"] is True
    assert note["total"] == -inv["total"]
    # T5: every tax component is negated, not just the total.
    assert note["taxable_value"] == -inv["taxable_value"]
    assert note["cgst"] == -inv["cgst"]
    assert note["sgst"] == -inv["sgst"]
    assert note["igst"] == -inv["igst"]
    assert note["original_invoice_id"] == inv["id"]
    assert note["invoice_no"].startswith("DRS/CN/")


async def test_generate_requires_delivered(client):
    admin = await login(client, "9999900001")
    r = await client.post(
        "/api/v1/admin/invoices/generate?delivery_order_id=999999", headers=auth(admin)
    )
    assert r.status_code == 404


async def test_invoice_scoping(client):
    admin = await login(client, "9999900001")
    other = await login(client, "9191919191")
    do_id = await _delivered_ghee_order(client, auth(admin))
    inv = (
        await client.post(
            f"/api/v1/admin/invoices/generate?delivery_order_id={do_id}", headers=auth(admin)
        )
    ).json()
    # Admin's invoice is not visible to an unrelated customer — by id, as a PDF link, or
    # in their list — while the owner (admin here) can see it (positive control).
    r = await client.get(f"/api/v1/invoices/{inv['id']}", headers=auth(other))
    assert r.status_code == 404
    assert (await client.get(f"/api/v1/invoices/{inv['id']}/pdf", headers=auth(other))).status_code == 404
    assert all(i["id"] != inv["id"] for i in (await client.get("/api/v1/invoices", headers=auth(other))).json())
    assert (await client.get(f"/api/v1/invoices/{inv['id']}", headers=auth(admin))).status_code == 200
