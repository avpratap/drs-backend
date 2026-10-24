from conftest import auth, login


async def _order_id(client, cust_h, qty=2) -> int:
    prods = (await client.get("/api/v1/products", headers=cust_h)).json()
    pid = next(p["id"] for p in prods if p["sku"] == "MILK-1L")
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": qty}], "address": "12 MG Road, Jaipur"},
        headers=cust_h,
    )
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    # A non-COD (prepaid) order must be PAID before it can ship — drive it through the
    # dev payment gateway so the realistic prepaid flow is exercised.
    create = await client.post(
        "/api/v1/payments/create", json={"purpose": "order_payment", "order_id": oid},
        headers=cust_h,
    )
    goid = create.json()["gateway_order_id"]
    await client.post(
        "/api/v1/payments/verify",
        json={"gateway_order_id": goid, "gateway_payment_id": "p", "signature": "dev"},
        headers=cust_h,
    )
    return oid


async def _create_shipment(client, admin_h, oid, **over):
    body = {"order_id": oid, "pincode": "302001", "name": "Aarav", "phone": "9876543210", "weight_kg": 1.0}
    body.update(over)
    return await client.post("/api/v1/admin/shipments", json=body, headers=admin_h)


async def test_serviceability_devstub(client):
    cust = await login(client, "9876543210")
    r = await client.get("/api/v1/shipping/serviceability?pincode=560001&weight_kg=1", headers=auth(cust))
    assert r.status_code == 200
    body = r.json()
    assert body["serviceable"] is True and body["courier"] == "DEV-Courier"


async def test_create_shipment_and_track(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    ch = auth(cust)
    oid = await _order_id(client, ch)

    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 200, r.text
    ship = r.json()
    assert ship["status"] == "created"
    assert ship["awb"].startswith("DEVAWB")
    assert ship["shipment_no"].startswith("SHP")
    assert any(e["status"] == "created" for e in ship["events"])
    sid, awb = ship["id"], ship["awb"]

    # Customer sees their own shipment.
    mine = await client.get(f"/api/v1/shipments/{sid}", headers=ch)
    assert mine.status_code == 200 and mine.json()["id"] == sid

    # Webhook advances status.
    w1 = await client.post("/api/v1/shipping/webhook", json={"awb": awb, "current_status": "In Transit"})
    assert w1.status_code == 200 and w1.json()["status"] == "ok"
    assert (await client.get(f"/api/v1/shipments/{sid}", headers=ah)).json()["status"] == "in_transit"

    # Deliver, then a duplicate delivered webhook is idempotent.
    await client.post("/api/v1/shipping/webhook", json={"awb": awb, "current_status": "Delivered"})
    dup = await client.post("/api/v1/shipping/webhook", json={"awb": awb, "current_status": "Delivered"})
    assert dup.json()["status"] == "duplicate"
    final = (await client.get(f"/api/v1/shipments/{sid}", headers=ah)).json()
    assert final["status"] == "delivered"


async def test_webhook_unknown_awb(client):
    r = await client.post("/api/v1/shipping/webhook", json={"awb": "NOSUCHAWB", "current_status": "Delivered"})
    assert r.status_code == 200 and r.json()["status"] == "unknown_awb"


async def test_shipment_idor_404(client):
    admin = await login(client, "9999900001")
    cust = await login(client, "9876543210")
    oid = await _order_id(client, auth(cust))
    ship = (await _create_shipment(client, auth(admin), oid)).json()
    # A different customer must not see it (404, not 403).
    other = await login(client, "9000000123")
    r = await client.get(f"/api/v1/shipments/{ship['id']}", headers=auth(other))
    assert r.status_code == 404


async def test_create_shipment_requires_admin(client):
    cust = await login(client, "9876543210")
    oid = await _order_id(client, auth(cust))
    r = await _create_shipment(client, auth(cust), oid)  # customer token
    assert r.status_code == 403


async def test_cancel_shipment(client):
    admin = await login(client, "9999900001")
    ah = auth(admin)
    cust = await login(client, "9876543210")
    oid = await _order_id(client, auth(cust))
    ship = (await _create_shipment(client, ah, oid)).json()
    c = await client.post(f"/api/v1/admin/shipments/{ship['id']}/cancel", headers=ah)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    # Cannot cancel a terminal shipment again.
    again = await client.post(f"/api/v1/admin/shipments/{ship['id']}/cancel", headers=ah)
    assert again.status_code == 409


async def _unpaid_order_id(client, cust_h, qty=1) -> int:
    prods = (await client.get("/api/v1/products", headers=cust_h)).json()
    pid = next(p["id"] for p in prods if p["sku"] == "MILK-1L")
    r = await client.post(
        "/api/v1/orders",
        json={"items": [{"product_id": pid, "qty": qty}], "address": "7 Station Rd, Jaipur"},
        headers=cust_h,
    )
    return r.json()["id"]


async def test_prepaid_unpaid_order_cannot_ship_but_cod_can(client):
    """#79: a non-COD shipment requires a paid order; a COD shipment may ship unpaid."""
    ah = auth(await login(client, "9999900001"))
    ch = auth(await login(client, "9876543210"))

    oid = await _unpaid_order_id(client, ch)
    blocked = await _create_shipment(client, ah, oid)  # non-COD, order still pending
    assert blocked.status_code == 409

    oid2 = await _unpaid_order_id(client, ch)
    cod_ok = await _create_shipment(client, ah, oid2, cod=True)
    assert cod_ok.status_code == 200, cod_ok.text


async def test_create_shipment_unknown_order_404(client):
    admin = await login(client, "9999900001")
    r = await _create_shipment(client, auth(admin), 999999)
    assert r.status_code == 404


# ---------- S1: no DB lock across the courier call; resumable drafts ----------
async def _ready_order(client) -> tuple[dict, int]:
    ah = auth(await login(client, "9999900001"))
    oid = await _order_id(client, auth(await login(client, "9876543210")))
    return ah, oid


async def _db(sql: str, **params):
    import sqlalchemy as sa

    from app.core.db import engine

    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), params)
        return res.all() if res.returns_rows else None


def _wrap_courier(monkeypatch, during=None, fail=False):
    """Patch the courier create; run `during(payload)` while the call is 'in flight'."""
    from app.services.shipping import client as sr

    real = sr.create_shipment
    calls = []

    async def fake(payload):
        calls.append(payload)
        if during is not None:
            await during(payload)
        if fail:
            raise RuntimeError("courier down")
        return await real(payload)

    monkeypatch.setattr(sr, "create_shipment", fake)
    return calls


def _spy_cancel(monkeypatch):
    from app.services.shipping import client as sr

    cancelled = []

    async def fake_cancel(gsid):
        cancelled.append(gsid)
        return True

    monkeypatch.setattr(sr, "cancel_shipment", fake_cancel)
    return cancelled


async def test_create_holds_no_lock_during_courier_call(client, monkeypatch):
    ah, oid = await _ready_order(client)
    seen = {}

    async def during(payload):
        # From ANOTHER connection: the draft is already committed and neither the order
        # nor the shipment row is locked (NOWAIT would raise if a lock were held).
        sid = payload["shipment_id"]
        rows = await _db("SELECT status, shipment_no FROM shipments WHERE id=:s", s=sid)
        seen["draft"] = tuple(rows[0])
        await _db("SELECT id FROM orders WHERE id=:o FOR UPDATE NOWAIT", o=oid)
        await _db("SELECT id FROM shipments WHERE id=:s FOR UPDATE NOWAIT", s=sid)

    calls = _wrap_courier(monkeypatch, during)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created" and body["awb"].startswith("DEVAWB")
    assert seen["draft"] == ("draft", body["shipment_no"])
    assert body["shipment_no"] == f"SHP{body['id']:06d}"
    assert calls[0]["order"]["order_id"] == body["shipment_no"]


async def test_courier_failure_leaves_resumable_draft(client, monkeypatch):
    ah, oid = await _ready_order(client)
    _wrap_courier(monkeypatch, fail=True)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 502
    rows = await _db(
        "SELECT id, status, shipment_no, create_lease_until FROM shipments WHERE order_id=:o", o=oid
    )
    assert len(rows) == 1
    sid, status, shipment_no, lease = rows[0]
    assert status == "draft" and lease is None  # claim released -> retry can resume now

    monkeypatch.undo()
    calls = _wrap_courier(monkeypatch)
    again = await _create_shipment(client, ah, oid)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == sid and again.json()["shipment_no"] == shipment_no
    assert calls[0]["order"]["order_id"] == shipment_no  # same courier key, no duplicate
    assert len(await _db("SELECT id FROM shipments WHERE order_id=:o", o=oid)) == 1
    # Now it's live: a further create is a plain conflict.
    assert (await _create_shipment(client, ah, oid)).status_code == 409


async def test_crashed_create_inflight_conflicts_then_resumes_after_lease(client):
    ah, oid = await _ready_order(client)
    acct = (await _db("SELECT account_id FROM orders WHERE id=:o", o=oid))[0][0]
    # A crash after phase A: a committed draft whose lease is still live.
    await _db(
        "INSERT INTO shipments (shipment_no, order_id, account_id, status, to_pincode, "
        "shipping_cost, cod_amount, weight_kg, create_lease_until) VALUES "
        "(:n, :o, :a, 'draft', '302001', 0, 0, 1, now() + interval '5 minutes')",
        n=f"CRASH-{oid}", o=oid, a=acct,
    )
    busy = await _create_shipment(client, ah, oid)
    assert busy.status_code == 409 and "in progress" in busy.json()["detail"]

    await _db("UPDATE shipments SET create_lease_until = now() - interval '1 second' WHERE order_id=:o", o=oid)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 200, r.text
    assert r.json()["shipment_no"] == f"CRASH-{oid}" and r.json()["status"] == "created"
    assert len(await _db("SELECT id FROM shipments WHERE order_id=:o", o=oid)) == 1


async def test_draft_cancelled_during_courier_call_cancels_orphan(client, monkeypatch):
    ah, oid = await _ready_order(client)

    async def during(payload):
        await _db("UPDATE shipments SET status='cancelled' WHERE id=:s", s=payload["shipment_id"])

    _wrap_courier(monkeypatch, during)
    cancelled = _spy_cancel(monkeypatch)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 409
    rows = await _db("SELECT id, status, awb FROM shipments WHERE order_id=:o", o=oid)
    assert rows[0][1] == "cancelled" and rows[0][2] is None  # not resurrected
    assert cancelled == [f"DEVSHP{rows[0][0]:08d}"]  # the courier order we made is dropped


async def test_order_cancelled_during_courier_call_does_not_ship(client, monkeypatch):
    ah, oid = await _ready_order(client)

    async def during(payload):
        await _db("UPDATE orders SET status='cancelled' WHERE id=:o", o=oid)

    _wrap_courier(monkeypatch, during)
    cancelled = _spy_cancel(monkeypatch)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 409
    rows = await _db("SELECT id, status FROM shipments WHERE order_id=:o", o=oid)
    assert rows[0][1] == "cancelled"
    assert cancelled == [f"DEVSHP{rows[0][0]:08d}"]


async def test_cancel_draft_skips_courier(client, monkeypatch):
    ah, oid = await _ready_order(client)
    _wrap_courier(monkeypatch, fail=True)
    assert (await _create_shipment(client, ah, oid)).status_code == 502
    sid = (await _db("SELECT id FROM shipments WHERE order_id=:o", o=oid))[0][0]
    cancelled = _spy_cancel(monkeypatch)
    c = await client.post(f"/api/v1/admin/shipments/{sid}/cancel", headers=ah)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert cancelled == []  # no courier shipment existed, nothing to cancel there


async def test_cancel_respects_terminal_state_set_during_courier_call(client, monkeypatch):
    """#74 phase C must see the row's CURRENT status (not the session's stale copy)."""
    from app.services.shipping import client as sr

    ah, oid = await _ready_order(client)
    ship = (await _create_shipment(client, ah, oid)).json()

    async def cancel_while_delivered(gsid):
        await _db("UPDATE shipments SET status='delivered' WHERE id=:s", s=ship["id"])
        return True

    monkeypatch.setattr(sr, "cancel_shipment", cancel_while_delivered)
    c = await client.post(f"/api/v1/admin/shipments/{ship['id']}/cancel", headers=ah)
    assert c.status_code == 409
    assert (await _db("SELECT status FROM shipments WHERE id=:s", s=ship["id"]))[0][0] == "delivered"
