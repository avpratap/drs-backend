import asyncio

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


# ---------- S1: no DB lock across the courier calls; resumable drafts ----------
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


async def _ships(oid):
    return await _db(
        "SELECT id, status, shipment_no, gateway_shipment_id, awb, create_lease_until "
        "FROM shipments WHERE order_id=:o ORDER BY id",
        o=oid,
    )


class _Courier:
    """Patch the two courier steps. `during_*` runs while that call is 'in flight'."""

    def __init__(self, monkeypatch, *, during_order=None, during_awb=None,
                 fail_order=False, fail_awb=False, cancel_ok=True):
        from app.services.shipping import client as sr

        self.orders, self.awbs, self.cancelled = [], [], []
        real_order, real_awb = sr.create_order, sr.assign_awb

        async def create_order(local_id, order):
            self.orders.append(order)
            if during_order:
                await during_order(local_id)
            if fail_order:
                raise RuntimeError("courier down")
            return await real_order(local_id, order)

        async def assign_awb(local_id, gsid):
            self.awbs.append(gsid)
            if during_awb:
                await during_awb(local_id)
            if fail_awb:
                raise RuntimeError("awb down")
            return await real_awb(local_id, gsid)

        async def cancel(gsid):
            self.cancelled.append(gsid)
            return cancel_ok

        monkeypatch.setattr(sr, "create_order", create_order)
        monkeypatch.setattr(sr, "assign_awb", assign_awb)
        monkeypatch.setattr(sr, "cancel_shipment", cancel)


async def test_create_holds_no_lock_during_courier_calls(client, monkeypatch):
    ah, oid = await _ready_order(client)
    seen = []

    async def unlocked(sid):
        # From ANOTHER connection: the draft is committed and neither the order nor the
        # shipment row is locked (NOWAIT raises if a lock were held).
        seen.append(tuple((await _db("SELECT status, gateway_shipment_id FROM shipments WHERE id=:s", s=sid))[0]))
        await _db("SELECT id FROM orders WHERE id=:o FOR UPDATE NOWAIT", o=oid)
        await _db("SELECT id FROM shipments WHERE id=:s FOR UPDATE NOWAIT", s=sid)

    c = _Courier(monkeypatch, during_order=unlocked, during_awb=unlocked)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created" and body["awb"] == f"DEVAWB{body['id']:08d}"
    assert body["shipment_no"] == f"SHP{body['id']:06d}"
    gsid = f"DEVSHP{body['id']:08d}"
    # During step 1 a bare draft; during step 2 the courier id is already persisted.
    assert seen == [("draft", None), ("draft", gsid)]
    assert c.orders[0]["order_id"] == body["shipment_no"] and c.awbs == [gsid]
    assert (await _ships(oid))[0][5] is None  # lease cleared on finalize


async def test_concurrent_creates_make_one_shipment_and_one_courier_order(client, monkeypatch):
    ah, oid = await _ready_order(client)

    async def slow(_sid):
        await asyncio.sleep(0.3)

    c = _Courier(monkeypatch, during_order=slow)
    r1, r2 = await asyncio.gather(_create_shipment(client, ah, oid), _create_shipment(client, ah, oid))
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], (r1.text, r2.text)
    loser = r1 if r1.status_code == 409 else r2
    assert "in progress" in loser.json()["detail"]
    assert len(await _ships(oid)) == 1 and len(c.orders) == 1


async def test_order_create_failure_leaves_resumable_draft(client, monkeypatch):
    ah, oid = await _ready_order(client)
    _Courier(monkeypatch, fail_order=True)
    assert (await _create_shipment(client, ah, oid)).status_code == 502
    [(sid, status, shipment_no, gsid, _awb, lease)] = await _ships(oid)
    assert status == "draft" and gsid is None and lease is None  # claim released

    monkeypatch.undo()
    c = _Courier(monkeypatch)
    again = await _create_shipment(client, ah, oid, address="New addr")
    assert again.status_code == 200, again.text
    assert again.json()["id"] == sid and again.json()["shipment_no"] == shipment_no
    assert c.orders[0]["order_id"] == shipment_no  # same courier key
    assert c.orders[0]["billing_address"] == "New addr"  # no courier order yet: re-snapshotted
    assert len(await _ships(oid)) == 1
    assert (await _create_shipment(client, ah, oid)).status_code == 409


async def test_awb_failure_resumes_without_recreating_courier_order(client, monkeypatch):
    """Partial courier success: the order exists, AWB failed. A retry must NOT create a
    second courier order — it resumes at AWB assignment with the persisted id."""
    ah, oid = await _ready_order(client)
    first = _Courier(monkeypatch, fail_awb=True)
    assert (await _create_shipment(client, ah, oid)).status_code == 502
    [(sid, status, _no, gsid, awb, lease)] = await _ships(oid)
    assert status == "draft" and gsid == f"DEVSHP{sid:08d}" and awb is None and lease is None
    assert len(first.orders) == 1

    monkeypatch.undo()
    c = _Courier(monkeypatch)
    r = await _create_shipment(client, ah, oid, address="Changed later")
    assert r.status_code == 200, r.text
    assert c.orders == [] and c.awbs == [gsid]  # no second courier order
    assert r.json()["status"] == "created" and r.json()["awb"] == f"DEVAWB{sid:08d}"
    to_addr = (await _db("SELECT to_address FROM shipments WHERE id=:s", s=sid))[0][0]
    assert to_addr is None  # kept the snapshot the courier order was created with


async def test_resume_gates_on_the_existing_courier_orders_payment_method(client, monkeypatch):
    """#79 on resume: a prepaid courier order on an UNPAID order can't be finalized by a
    retry that claims cod=True."""
    ah = auth(await login(client, "9999900001"))
    oid = await _unpaid_order_id(client, auth(await login(client, "9876543210")))
    acct = (await _db("SELECT account_id FROM orders WHERE id=:o", o=oid))[0][0]
    await _db(
        "INSERT INTO shipments (shipment_no, order_id, account_id, status, to_pincode, "
        "shipping_cost, cod_amount, weight_kg, gateway_shipment_id) VALUES "
        "(:n, :o, :a, 'draft', '302001', 0, 0, 1, 'GSID-PREPAID')",
        n=f"PRE-{oid}", o=oid, a=acct,
    )
    c = _Courier(monkeypatch)
    r = await _create_shipment(client, ah, oid, cod=True)
    assert r.status_code == 409 and "Prepaid" in r.json()["detail"]
    assert c.awbs == []


async def test_crashed_create_inflight_conflicts_then_resumes_after_lease(client, monkeypatch):
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
    c = _Courier(monkeypatch)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 200, r.text
    assert r.json()["shipment_no"] == f"CRASH-{oid}" and r.json()["status"] == "created"
    assert c.orders[0]["order_id"] == f"CRASH-{oid}" and len(await _ships(oid)) == 1


async def test_draft_cancelled_during_order_create_cancels_orphan(client, monkeypatch):
    ah, oid = await _ready_order(client)

    async def cancel_it(sid):
        await _db("UPDATE shipments SET status='cancelled' WHERE id=:s", s=sid)

    c = _Courier(monkeypatch, during_order=cancel_it)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 409 and "cancelled" in r.json()["detail"]
    [(sid, status, _no, gsid, awb, _lease)] = await _ships(oid)
    assert status == "cancelled" and gsid is None and awb is None  # not resurrected
    assert c.cancelled == [f"DEVSHP{sid:08d}"] and c.awbs == []  # our orphan dropped


async def test_admin_cancel_during_awb_step_cancels_courier_once(client, monkeypatch):
    """Real concurrent cancel_shipment (own session) while AWB assignment is in flight:
    cancel wins, the persisted courier order is cancelled exactly once, create says 409."""
    from app.core.db import SessionLocal
    from app.services.shipping import service as ship_svc

    ah, oid = await _ready_order(client)
    out = {}

    async def admin_cancels(sid):
        async with SessionLocal() as db2:
            out["cancel"] = (await ship_svc.cancel_shipment(db2, sid)).status

    c = _Courier(monkeypatch, during_awb=admin_cancels)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 409 and out["cancel"] == "cancelled"
    [(_sid, status, _no, gsid, awb, _lease)] = await _ships(oid)
    assert status == "cancelled" and awb is None
    assert c.cancelled == [gsid]


async def test_order_cancelled_during_awb_step_does_not_ship(client, monkeypatch):
    ah, oid = await _ready_order(client)

    async def cancel_order(_sid):
        await _db("UPDATE orders SET status='cancelled' WHERE id=:o", o=oid)

    c = _Courier(monkeypatch, during_awb=cancel_order)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 409
    [(_sid, status, _no, gsid, awb, _lease)] = await _ships(oid)
    assert status == "cancelled" and awb is None
    assert c.cancelled == [gsid]  # the persisted courier order is dropped


async def test_lost_lease_bails_out_and_never_clears_anothers_claim(client, monkeypatch):
    ah, oid = await _ready_order(client)

    async def taken_over(sid):  # our lease expired and another create re-claimed it
        await _db("UPDATE shipments SET create_lease_until = now() + interval '10 minutes' WHERE id=:s", s=sid)

    c = _Courier(monkeypatch, during_order=taken_over)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 409 and "in progress" in r.json()["detail"]
    [(sid, status, _no, gsid, _awb, lease)] = await _ships(oid)
    assert status == "draft" and gsid is None and lease is not None  # theirs, untouched
    assert c.cancelled == [f"DEVSHP{sid:08d}"]  # our unpersisted courier order dropped

    # A failing call must not release a claim it no longer owns either.
    await _db("UPDATE shipments SET create_lease_until = now() - interval '1 second' WHERE id=:s", s=sid)
    monkeypatch.undo()
    _Courier(monkeypatch, during_order=taken_over, fail_order=True)
    assert (await _create_shipment(client, ah, oid)).status_code == 502
    assert (await _ships(oid))[0][5] is not None


async def test_superseded_by_finished_create_drops_orphan_even_if_cancel_fails(
    client, monkeypatch, caplog
):
    ah, oid = await _ready_order(client)

    async def other_create_finished(sid):
        await _db(
            "UPDATE shipments SET status='created', gateway_shipment_id='GS-OTHER', "
            "awb='AWB-OTHER' WHERE id=:s", s=sid,
        )

    c = _Courier(monkeypatch, during_order=other_create_finished, cancel_ok=False)
    r = await _create_shipment(client, ah, oid)
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    [(sid, status, _no, gsid, _awb, _lease)] = await _ships(oid)
    assert status == "created" and gsid == "GS-OTHER"  # the winner is untouched
    assert c.cancelled == [f"DEVSHP{sid:08d}"]  # only OUR courier order is cancelled
    assert "could not be cancelled" in caplog.text  # a failed cleanup is surfaced


async def test_cancel_draft_skips_courier(client, monkeypatch):
    ah, oid = await _ready_order(client)
    _Courier(monkeypatch, fail_order=True)
    assert (await _create_shipment(client, ah, oid)).status_code == 502
    sid = (await _ships(oid))[0][0]
    monkeypatch.undo()
    c = _Courier(monkeypatch)
    r = await client.post(f"/api/v1/admin/shipments/{sid}/cancel", headers=ah)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert c.cancelled == []  # no courier shipment existed, nothing to cancel there
    # The order can then get a fresh shipment.
    assert (await _create_shipment(client, ah, oid)).status_code == 200


async def test_cancel_refuses_when_create_persists_courier_order_meanwhile(client, monkeypatch):
    """Cancel read a draft with no courier id; a create finalized before cancel's phase C.
    Cancel must not mark it cancelled (that would orphan a live AWB at the courier)."""
    from app.services.shipping import service as ship_svc

    ah, oid = await _ready_order(client)
    _Courier(monkeypatch, fail_order=True)
    assert (await _create_shipment(client, ah, oid)).status_code == 502
    sid = (await _ships(oid))[0][0]
    monkeypatch.undo()

    real_relock = ship_svc._relock

    async def relock_after_create_finalized(db, stmt):
        await _db(
            "UPDATE shipments SET status='created', gateway_shipment_id='GS-NEW', awb='AWB-NEW' "
            "WHERE id=:s", s=sid,
        )
        return await real_relock(db, stmt)

    monkeypatch.setattr(ship_svc, "_relock", relock_after_create_finalized)
    r = await client.post(f"/api/v1/admin/shipments/{sid}/cancel", headers=ah)
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert (await _ships(oid))[0][1] == "created"


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


async def test_customer_never_sees_a_draft(client, monkeypatch):
    ah = auth(await login(client, "9999900001"))
    ch = auth(await login(client, "9876543210"))
    oid = await _order_id(client, ch)
    _Courier(monkeypatch, fail_order=True)
    assert (await _create_shipment(client, ah, oid)).status_code == 502
    sid = (await _ships(oid))[0][0]
    assert (await client.get(f"/api/v1/shipments/{sid}", headers=ch)).status_code == 404
    assert (await client.get(f"/api/v1/shipments?order_id={oid}", headers=ch)).json() == []
    tracking = await client.get(f"/api/v1/orders/{oid}/tracking", headers=ch)
    assert tracking.status_code == 200 and tracking.json()["shipments"] == []
    # Admin still sees it (to resume or cancel).
    admin_view = await client.get(f"/api/v1/shipments/{sid}", headers=ah)
    assert admin_view.status_code == 200 and admin_view.json()["status"] == "draft"


async def test_live_client_rejects_incomplete_courier_responses(monkeypatch):
    """Real-mode parsing: a missing shipment_id / awb_code is an error (draft stays
    resumable), never a 'created' shipment without an AWB."""
    import pytest

    from app.services.shipping import client as sr

    class _Resp:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    bodies = {}

    async def fake_authed(method, path, **kw):
        return _Resp(bodies[path])

    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(sr, "_authed", fake_authed)

    bodies["/orders/create/adhoc"] = {"order_id": 11, "shipment_id": 22}
    assert await sr.create_order(1, {}) == {"gateway_order_id": "11", "gateway_shipment_id": "22"}
    bodies["/orders/create/adhoc"] = {"status_code": 422, "message": "bad pincode"}
    with pytest.raises(sr.ShippingError):
        await sr.create_order(1, {})

    bodies["/courier/assign/awb"] = {"response": {"data": {"awb_code": "A1", "courier_name": "X"}}}
    assert (await sr.assign_awb(1, "22"))["awb"] == "A1"
    bodies["/courier/assign/awb"] = {"awb_assign_status": 0, "response": {"data": {}}}
    with pytest.raises(sr.ShippingError):
        await sr.assign_awb(1, "22")
