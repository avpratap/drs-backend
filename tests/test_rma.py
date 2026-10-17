from conftest import auth, login


async def _milk_id(client, h) -> int:
    prods = (await client.get("/api/v1/products", headers=h)).json()
    return next(p["id"] for p in prods if p["sku"] == "MILK-1L")


async def _order(client, h, qty=3) -> tuple[int, int]:
    pid = await _milk_id(client, h)
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": qty}], "address": "9 Civil Lines, Jaipur"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"], pid


async def _wallet_balance(client, h) -> float:
    return (await client.get("/api/v1/billing/me", headers=h)).json()["wallet_balance"]


async def test_rma_full_flow_refund_credits_wallet(client):
    cust = await login(client, "9876543210")
    ch = auth(cust)
    admin = await login(client, "9999900001")
    ah = auth(admin)
    oid, pid = await _order(client, ch)
    start_bal = await _wallet_balance(client, ch)

    r = await client.post(
        "/api/v1/returns",
        json={"order_id": oid, "items": [{"product_id": pid, "qty": 2, "reason": "spoiled"}], "reason": "quality"},
        headers=ch,
    )
    assert r.status_code == 200, r.text
    rr = r.json()
    assert rr["status"] == "requested" and rr["rma_no"].startswith("RMA")
    rid = rr["id"]

    # Can't refund before approval.
    assert (await client.post(f"/api/v1/admin/returns/{rid}/refund", json={"amount": 54}, headers=ah)).status_code == 409

    assert (await client.post(f"/api/v1/admin/returns/{rid}/approve", headers=ah)).json()["status"] == "approved"
    ref = await client.post(f"/api/v1/admin/returns/{rid}/refund", json={"amount": 54}, headers=ah)
    assert ref.status_code == 200 and ref.json()["status"] == "refunded"
    assert await _wallet_balance(client, ch) == round(start_bal + 54, 2)


async def test_rma_reject(client):
    cust = await login(client, "9876543210")
    ch = auth(cust)
    ah = auth(await login(client, "9999900001"))
    oid, pid = await _order(client, ch)
    rid = (await client.post(
        "/api/v1/returns",
        json={"order_id": oid, "items": [{"product_id": pid, "qty": 1}]},
        headers=ch,
    )).json()["id"]
    rej = await client.post(f"/api/v1/admin/returns/{rid}/reject", json={"note": "outside window"}, headers=ah)
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    assert rej.json()["resolution"] == "outside window"


async def test_rma_cancel_while_requested(client):
    cust = await login(client, "9876543210")
    ch = auth(cust)
    oid, pid = await _order(client, ch)
    rid = (await client.post(
        "/api/v1/returns",
        json={"order_id": oid, "items": [{"product_id": pid, "qty": 1}]},
        headers=ch,
    )).json()["id"]
    c = await client.post(f"/api/v1/returns/{rid}/cancel", headers=ch)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"


async def test_rma_product_not_on_order_422(client):
    cust = await login(client, "9876543210")
    ch = auth(cust)
    admin = await login(client, "9999900001")
    # fresh product the customer never ordered
    other_pid = (await client.post("/api/v1/products", json={"sku": "RMA-X", "name": "X"}, headers=auth(admin))).json()["id"]
    oid, _ = await _order(client, ch)
    r = await client.post(
        "/api/v1/returns",
        json={"order_id": oid, "items": [{"product_id": other_pid, "qty": 1}]},
        headers=ch,
    )
    assert r.status_code == 422


async def test_rma_qty_exceeds_ordered_422(client):
    cust = await login(client, "9876543210")
    ch = auth(cust)
    oid, pid = await _order(client, ch, qty=2)
    r = await client.post(
        "/api/v1/returns",
        json={"order_id": oid, "items": [{"product_id": pid, "qty": 5}]},
        headers=ch,
    )
    assert r.status_code == 422


async def test_rma_idor_and_admin_gate(client):
    cust = await login(client, "9876543210")
    ch = auth(cust)
    oid, pid = await _order(client, ch)
    rid = (await client.post(
        "/api/v1/returns",
        json={"order_id": oid, "items": [{"product_id": pid, "qty": 1}]},
        headers=ch,
    )).json()["id"]
    # Another customer can't see it.
    other = auth(await login(client, "9000000456"))
    assert (await client.get(f"/api/v1/returns/{rid}", headers=other)).status_code == 404
    # Customer can't hit the admin list.
    assert (await client.get("/api/v1/admin/returns", headers=ch)).status_code == 403


async def test_rma_unknown_order_404(client):
    cust = await login(client, "9876543210")
    r = await client.post(
        "/api/v1/returns",
        json={"order_id": 999999, "items": [{"product_id": 1, "qty": 1}]},
        headers=auth(cust),
    )
    assert r.status_code == 404
