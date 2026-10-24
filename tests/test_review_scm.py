"""Regression tests for the review's delivery & supply-chain fixes (D*), see
scratchpad/MASTER_FIXLIST.md. Every test uses its own fresh product so stock state is
isolated from the rest of the suite."""
import asyncio
import datetime as dt
import time
import uuid

import pytest
import sqlalchemy as sa
from conftest import auth, idem, login

from app.core.db import SessionLocal, engine

TODAY = dt.datetime.now(dt.UTC).date()
YESTERDAY = TODAY - dt.timedelta(days=2)  # safely "expired" in both UTC and IST
NEXT_MONTH = TODAY + dt.timedelta(days=30)


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _admin(client) -> dict:
    return auth(await login(client, "9999900001"))


async def _product(client, ah) -> int:
    r = await client.post("/api/v1/products", json={
        "sku": f"T-{uuid.uuid4().hex[:10]}", "name": "Review test item", "uom": "litre"}, headers=ah)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _loc(client, ah, **extra) -> int:
    r = await client.post("/api/v1/admin/locations", json={
        "name": f"L-{uuid.uuid4().hex[:6]}", **extra}, headers=ah)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _receive(client, ah, pid, qty, expiry, loc=None, batch=None):
    body = {"product_id": pid, "batch_no": batch or uuid.uuid4().hex[:8], "qty": qty,
            "expiry_date": expiry.isoformat()}
    if loc:
        body["location_id"] = loc
    r = await client.post("/api/v1/admin/stock/batches", json=body, headers=idem(ah))
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _on_hand(pid, loc) -> float:
    rows = await _q("SELECT qty_on_hand FROM stock_items WHERE product_id=:p AND location_id=:l", p=pid, l=loc)
    return float(rows[0][0]) if rows else 0.0


async def _active_lot_qty(pid, loc) -> float:
    return float((await _q(
        "SELECT COALESCE(SUM(qty),0) FROM stock_batches WHERE product_id=:p AND location_id=:l AND status='active'",
        p=pid, l=loc))[0][0])


# ---------- D1: unswept expired lots are not double-counted ----------
async def test_consume_writes_off_expired_lot_first_no_double_count(client):
    from app.services import batches

    ah = await _admin(client)
    pid = await _product(client, ah)
    loc = await _loc(client, ah)
    await _receive(client, ah, pid, 10, YESTERDAY, loc)   # expired, not yet swept
    await _receive(client, ah, pid, 5, NEXT_MONTH, loc)    # fresh
    async with SessionLocal() as db:
        await batches.consume_fefo(db, pid, 5, "delivery", location_id=loc)
        await db.commit()
    assert await _on_hand(pid, loc) == 0.0  # 15 - 10 expired - 5 delivered
    # The later sweep finds nothing left to write off (was: on-hand -10).
    async with SessionLocal() as db:
        res = await batches.expire_batches(db, TODAY)
        await db.commit()
    assert await _on_hand(pid, loc) == 0.0
    # This product's expired lot was already written off by the consume, so the sweep
    # can't remove it again (other tests' lots may be swept in the same call).
    [(st, q)] = await _q("SELECT status, qty FROM stock_batches WHERE product_id=:p AND expiry_date=:e",
                         p=pid, e=YESTERDAY)
    assert st == "expired" and float(q) == 0.0 and res is not None


async def test_production_gate_ignores_expired_component(client):
    ah = await _admin(client)
    comp = await _product(client, ah)
    out = await _product(client, ah)
    loc = await _loc(client, ah)
    await _receive(client, ah, comp, 10, YESTERDAY, loc)
    bom = (await client.post("/api/v1/admin/boms", json={
        "product_id": out, "name": "b", "output_qty": 1,
        "components": [{"component_product_id": comp, "qty": 1}]}, headers=ah)).json()["id"]
    pro = (await client.post("/api/v1/admin/production-orders", json={
        "bom_id": bom, "planned_qty": 10, "location_id": loc}, headers=ah)).json()["id"]
    r = await client.post(f"/api/v1/admin/production-orders/{pro}/start", headers=ah)
    assert r.status_code == 422  # nothing usable — expired milk is not stock
    assert await _on_hand(comp, loc) == 10.0  # rejected → nothing changed


# ---------- D3: transfers never ship expired lots ----------
async def test_transfer_ships_fresh_lot_not_expired(client):
    ah = await _admin(client)
    pid = await _product(client, ah)
    src = await _loc(client, ah)
    dst = await _loc(client, ah)
    await _receive(client, ah, pid, 5, YESTERDAY, src, batch="OLD")
    await _receive(client, ah, pid, 5, NEXT_MONTH, src, batch="NEW")
    tr = (await client.post("/api/v1/admin/transfers", json={
        "from_location_id": src, "to_location_id": dst, "items": [{"product_id": pid, "qty": 5}]},
        headers=ah)).json()["id"]
    assert (await client.post(f"/api/v1/admin/transfers/{tr}/dispatch", headers=ah)).status_code == 200
    assert (await client.post(f"/api/v1/admin/transfers/{tr}/receive", headers=ah)).status_code == 200
    lots = await _q("SELECT batch_no, status FROM stock_batches WHERE product_id=:p AND location_id=:l",
                    p=pid, l=dst)
    assert lots == [("NEW", "active")]
    assert await _on_hand(pid, src) == 0.0  # expired OLD written off, NEW moved


# ---------- D4: wastage / downward adjustments go through the lots ----------
async def test_wastage_reduces_lots_and_cannot_exceed_on_hand(client):
    ah = await _admin(client)
    pid = await _product(client, ah)
    loc = await _loc(client, ah)
    await _receive(client, ah, pid, 10, NEXT_MONTH, loc)
    too_much = await client.post("/api/v1/admin/stock/wastage", json={
        "product_id": pid, "qty": 25, "location_id": loc}, headers=idem(ah))
    assert too_much.status_code == 422
    ok = await client.post("/api/v1/admin/stock/wastage", json={
        "product_id": pid, "qty": 4, "location_id": loc}, headers=idem(ah))
    assert ok.status_code == 200 and ok.json()["qty_on_hand"] == 6.0
    adj = await client.post("/api/v1/admin/stock/adjust", json={
        "product_id": pid, "change": -2, "reason": "adjustment", "location_id": loc}, headers=idem(ah))
    assert adj.status_code == 200 and adj.json()["qty_on_hand"] == 4.0
    # Lots stay equal to on-hand (no phantom FEFO units).
    assert await _active_lot_qty(pid, loc) == await _on_hand(pid, loc) == 4.0
    below = await client.post("/api/v1/admin/stock/adjust", json={
        "product_id": pid, "change": -5, "reason": "adjustment", "location_id": loc}, headers=idem(ah))
    assert below.status_code == 422


# ---------- D5: stock writes require an Idempotency-Key ----------
async def test_stock_writes_require_idempotency_key(client):
    ah = await _admin(client)
    pid = await _product(client, ah)
    for path, body in (
        ("stock/adjust", {"product_id": pid, "change": 1, "reason": "receipt"}),
        ("stock/wastage", {"product_id": pid, "qty": 1}),
        ("stock/batches", {"product_id": pid, "batch_no": "X", "qty": 1}),
    ):
        assert (await client.post(f"/api/v1/admin/{path}", json=body, headers=ah)).status_code == 428


# ---------- D6: a driver's delivery deducts from their vehicle ----------
async def test_driver_delivery_deducts_vehicle_stock(client):
    ah = await _admin(client)
    driver = await login(client, "9999900003")
    did = (await _q("SELECT id FROM users WHERE phone='9999900003'"))[0][0]
    van = await _loc(client, ah, kind="vehicle", driver_user_id=did)
    milk = (await _q("SELECT id FROM products WHERE sku='MILK-1L'"))[0][0]
    await _receive(client, ah, milk, 50, NEXT_MONTH, van)
    d = "2040-01-05"
    await client.post(f"/api/v1/admin/cutoff/run?date={d}", json={}, headers=ah)
    orders = (await client.get(f"/api/v1/delivery/orders?date={d}", headers=auth(driver))).json()
    order = next(o for o in orders if o["status"] == "generated")
    lines = [{"product_id": i["product_id"], "qty_delivered": i["qty_planned"]} for i in order["items"]]
    r = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver",
                          json={"items": lines, "returns": []}, headers=auth(driver))
    assert r.status_code == 200, r.text
    planned = sum(i["qty_planned"] for i in order["items"] if i["product_id"] == milk)
    assert await _on_hand(milk, van) == 50 - planned
    # A vehicle binding is only valid for a delivery user.
    bad = await client.post("/api/v1/admin/locations", json={
        "name": "bad", "kind": "warehouse", "driver_user_id": did}, headers=ah)
    assert bad.status_code == 422


# ---------- D7: route optimisation is bounded ----------
async def test_route_optimisation_fast_and_capped():
    import random

    from app.services.ml import routing

    rnd = random.Random(7)
    pts = [{"id": i, "lat": 26.9 + rnd.random() * 0.2, "lng": 75.7 + rnd.random() * 0.2} for i in range(200)]
    t = time.monotonic()
    res = await routing.optimize_route(pts)
    assert time.monotonic() - t < 3.0  # was ~28s
    assert sorted(s["id"] for s in res["ordered"]) == list(range(200))
    with pytest.raises(ValueError):
        await routing.optimize_route([{"id": i, "lat": 1.0, "lng": 1.0} for i in range(routing.MAX_STOPS + 1)])


def test_two_opt_improves_or_keeps_route():
    import random

    from app.services.ml import routing

    rnd = random.Random(3)
    coords = [(26.9 + rnd.random() * 0.2, 75.7 + rnd.random() * 0.2) for _ in range(40)]
    nn = routing._nearest_neighbour(coords, 0)
    opt = routing._two_opt(nn, coords)
    assert opt[0] == 0 and sorted(opt) == list(range(40))
    assert routing._route_length(opt, coords) <= routing._route_length(nn, coords) + 1e-9


# ---------- D8: unknown location is a 404, not a 500 ----------
async def test_production_unknown_location_404(client):
    ah = await _admin(client)
    comp = await _product(client, ah)
    out = await _product(client, ah)
    bom = (await client.post("/api/v1/admin/boms", json={
        "product_id": out, "name": "b", "components": [{"component_product_id": comp, "qty": 1}]},
        headers=ah)).json()["id"]
    r = await client.post("/api/v1/admin/production-orders", json={
        "bom_id": bom, "planned_qty": 1, "location_id": 99999999}, headers=ah)
    assert r.status_code == 404


# ---------- D9: no return while the order is still in transit ----------
async def test_return_refused_while_shipment_in_transit(client):
    ah = await _admin(client)
    cust = await login(client, "9000000920")
    milk = (await _q("SELECT id FROM products WHERE sku='MILK-1L'"))[0][0]
    o = (await client.post("/api/v1/orders", json={"items": [{"product_id": milk, "qty": 1}]},
                           headers=auth(cust))).json()
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "order_payment", "order_id": o["id"]},
                             headers=auth(cust))).json()
    await client.post("/api/v1/payments/verify", json={
        "gateway_order_id": pay["gateway_order_id"], "gateway_payment_id": "p", "signature": "dev"},
        headers=auth(cust))
    s = await client.post("/api/v1/admin/shipments", json={"order_id": o["id"], "pincode": "302001"}, headers=ah)
    assert s.status_code == 200, s.text
    r = await client.post("/api/v1/returns", json={"order_id": o["id"], "items": [
        {"product_id": milk, "qty": 1}]}, headers=auth(cust))
    assert r.status_code == 409
    # After delivery the return is accepted.
    await client.post("/api/v1/shipping/webhook", json={"awb": s.json()["awb"], "current_status": "Delivered"})
    r2 = await client.post("/api/v1/returns", json={"order_id": o["id"], "items": [
        {"product_id": milk, "qty": 1}]}, headers=auth(cust))
    assert r2.status_code == 200, r2.text


# ---------- D2: concurrent receive + consume on one product never deadlock ----------
async def test_concurrent_receive_and_consume_no_deadlock(client):
    from app.services import batches

    ah = await _admin(client)
    pid = await _product(client, ah)
    loc = await _loc(client, ah)
    await _receive(client, ah, pid, 100, NEXT_MONTH, loc, batch="LOT")

    async def recv():
        async with SessionLocal() as db:
            await batches.receive_batch(db, pid, "LOT", 1, location_id=loc, expiry_date=NEXT_MONTH)
            await db.commit()

    async def take():
        async with SessionLocal() as db:
            await batches.consume_fefo(db, pid, 1, "delivery", location_id=loc)
            await db.commit()

    await asyncio.gather(*[f() for _ in range(5) for f in (recv, take)])
    assert await _on_hand(pid, loc) == 100.0
    assert await _active_lot_qty(pid, loc) == 100.0


# ---------- ledger invariant: movements always sum to on-hand ----------
async def test_movements_sum_to_on_hand_across_all_paths(client):
    from app.services import batches

    ah = await _admin(client)
    pid = await _product(client, ah)
    loc = await _loc(client, ah)
    await _receive(client, ah, pid, 20, NEXT_MONTH, loc)
    await _receive(client, ah, pid, 5, YESTERDAY, loc)  # will be written off on consume
    async with SessionLocal() as db:
        await batches.consume_fefo(db, pid, 3, "delivery", location_id=loc)
        await db.commit()
    await client.post("/api/v1/admin/stock/wastage", json={"product_id": pid, "qty": 2, "location_id": loc},
                      headers=idem(ah))
    await client.post("/api/v1/admin/stock/adjust", json={
        "product_id": pid, "change": -1, "reason": "adjustment", "location_id": loc}, headers=idem(ah))
    await client.post("/api/v1/admin/stock/adjust", json={
        "product_id": pid, "change": 4, "reason": "receipt", "location_id": loc}, headers=idem(ah))
    total = float((await _q("SELECT COALESCE(SUM(change),0) FROM stock_movements WHERE product_id=:p AND location_id=:l",
                            p=pid, l=loc))[0][0])
    assert total == await _on_hand(pid, loc) == 20 + 5 - 5 - 3 - 2 - 1 + 4
