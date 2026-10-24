"""Regression tests for the review's cutoff & subscription fixes (C*), see
scratchpad/MASTER_FIXLIST.md."""
import asyncio
import datetime as dt

import pytest
import sqlalchemy as sa
from conftest import auth, login

from app.core.db import engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _milk(client, token) -> int:
    prods = (await client.get("/api/v1/products", headers=auth(token))).json()
    return next(p["id"] for p in prods if p["sku"] == "MILK-1L")


async def _admin_sub(client, ah, name, qty=2) -> tuple[int, int]:
    shop = (await client.post("/api/v1/shops", json={"name": name, "state": "Rajasthan"}, headers=ah)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=ah)).json()["id"]
    milk = await _milk(client, (ah["Authorization"][7:]))
    r = await client.put(f"/api/v1/subscriptions/{sid}/items",
                         json=[{"product_id": milk, "default_qty": qty, "frequency": "daily"}], headers=ah)
    assert r.status_code == 200, r.text
    return shop, sid


async def _run(client, ah, date):
    r = await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=ah)
    assert r.status_code == 200, r.text
    return r.json()


async def _orders_for(shop, date):
    return await _q("SELECT id, status FROM delivery_orders WHERE shop_id=:s AND delivery_date=:d",
                    s=shop, d=dt.date.fromisoformat(date))


# ---------- C1: a driver's route scope is read-only ----------
async def test_driver_cannot_mutate_customer_subscription(client):
    driver = auth(await login(client, "9999900003"))
    [(sid, status)] = await _q(
        "SELECT s.id, s.status FROM subscriptions s JOIN shops sh ON sh.id=s.shop_id WHERE sh.name='Corner Store'"
    )
    assert (await client.get(f"/api/v1/subscriptions/{sid}", headers=driver)).status_code == 200  # read ok
    for path in ("pause", "cancel", "resume"):
        assert (await client.post(f"/api/v1/subscriptions/{sid}/{path}", headers=driver)).status_code == 404
    assert (await client.put(f"/api/v1/subscriptions/{sid}/items", json=[], headers=driver)).status_code == 404
    ov = await client.post(f"/api/v1/subscriptions/{sid}/overrides", json={
        "product_id": 1, "for_date": "2039-01-01", "override_qty": 99}, headers=driver)
    assert ov.status_code == 404
    assert (await _q("SELECT status FROM subscriptions WHERE id=:i", i=sid))[0][0] == status


async def test_status_change_notifies_account_owner_not_actor(client):
    ah = auth(await login(client, "9999900001"))
    cust = await login(client, "9000000901")
    r = await client.post("/api/v1/subscriptions", json={"home_address": "9 Lane", "home_state": "RJ"},
                          headers=auth(cust))
    sid = r.json()["id"]
    assert (await client.post(f"/api/v1/subscriptions/{sid}/pause", headers=ah)).status_code == 200
    rows = await _q(
        "SELECT u.phone FROM notifications n JOIN users u ON u.id=n.user_id "
        "WHERE n.event_type='subscription.paused' AND n.account_id=(SELECT account_id FROM subscriptions WHERE id=:i)",
        i=sid,
    )
    assert rows and {p for (p,) in rows} == {"9000000901"}


# ---------- C2: changes after an earlier run remove the stale generated order ----------
async def test_rerun_drops_order_after_skip_or_pause(client):
    ah = auth(await login(client, "9999900001"))
    shop, sid = await _admin_sub(client, ah, "C2 Skip Shop")
    d = "2039-02-01"
    await _run(client, ah, d)
    assert len(await _orders_for(shop, d)) == 1
    milk = await _milk(client, ah["Authorization"][7:])
    await client.post(f"/api/v1/subscriptions/{sid}/overrides",
                      json={"product_id": milk, "for_date": d, "override_qty": 0}, headers=ah)
    res = await _run(client, ah, d)
    assert await _orders_for(shop, d) == [] and res["orders_removed"] >= 1

    d2 = "2039-02-02"
    await _run(client, ah, d2)
    assert len(await _orders_for(shop, d2)) == 1
    await client.post(f"/api/v1/subscriptions/{sid}/pause", headers=ah)
    await _run(client, ah, d2)
    assert await _orders_for(shop, d2) == []


async def test_rerun_never_drops_a_delivered_order(client):
    ah = auth(await login(client, "9999900001"))
    shop, sid = await _admin_sub(client, ah, "C2 Delivered Shop")
    d = "2039-02-03"
    await _run(client, ah, d)
    [(oid, _)] = await _orders_for(shop, d)
    await _q("UPDATE delivery_orders SET status='delivered' WHERE id=:i", i=oid)
    await client.post(f"/api/v1/subscriptions/{sid}/pause", headers=ah)
    await _run(client, ah, d)
    assert await _orders_for(shop, d) == [(oid, "delivered")]


# ---------- C3: standing-order changes frozen after the cutoff hour ----------
async def test_standing_changes_frozen_after_cutoff(client, monkeypatch):
    from app.core.config import settings

    cust = await login(client, "9000000902")
    sid = (await client.post("/api/v1/subscriptions", json={"home_address": "1 Rd"},
                             headers=auth(cust))).json()["id"]
    milk = await _milk(client, cust)
    monkeypatch.setattr(settings, "CUTOFF_HOUR", 0)  # "now" is always past the cutoff
    put = await client.put(f"/api/v1/subscriptions/{sid}/items",
                           json=[{"product_id": milk, "default_qty": 3}], headers=auth(cust))
    assert put.status_code == 409
    assert (await client.post(f"/api/v1/subscriptions/{sid}/pause", headers=auth(cust))).status_code == 409
    assert (await _q("SELECT count(*) FROM subscription_items WHERE subscription_id=:i", i=sid))[0][0] == 0
    # Admins may still correct a standing order.
    ah = auth(await login(client, "9999900001"))
    assert (await client.put(f"/api/v1/subscriptions/{sid}/items",
                             json=[{"product_id": milk, "default_qty": 3}], headers=ah)).status_code == 200


# ---------- C4/C5: item validation ----------
async def test_set_items_rejects_duplicates_unknown_and_unpriced(client):
    ah = auth(await login(client, "9999900001"))
    _shop, sid = await _admin_sub(client, ah, "C4 Shop")
    milk = await _milk(client, ah["Authorization"][7:])
    dup = await client.put(f"/api/v1/subscriptions/{sid}/items", json=[
        {"product_id": milk, "default_qty": 1}, {"product_id": milk, "default_qty": 2}], headers=ah)
    assert dup.status_code == 422
    unknown = await client.put(f"/api/v1/subscriptions/{sid}/items",
                               json=[{"product_id": 999999, "default_qty": 1}], headers=ah)
    assert unknown.status_code == 404
    ghee = (await _q("SELECT id FROM products WHERE sku='GHEE-1KG'"))[0][0]
    unpriced = await client.put(f"/api/v1/subscriptions/{sid}/items",  # below ghee's min_qty=1
                                json=[{"product_id": ghee, "default_qty": 0.5}], headers=ah)
    assert unpriced.status_code == 422
    items = await _q("SELECT product_id, default_qty FROM subscription_items WHERE subscription_id=:i AND active",
                     i=sid)
    assert [(p, float(q)) for p, q in items] == [(milk, 2.0)]  # unchanged


# ---------- C6/C7: failures are visible, never 'frozen' / stuck 'generating' ----------
async def test_partial_failure_marks_run_partial(client, monkeypatch):
    from app.services import cutoff

    ah = auth(await login(client, "9999900001"))
    await _admin_sub(client, ah, "C6 Shop")
    real = cutoff._materialize_one
    calls = {"n": 0}

    async def flaky(db, sub, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return await real(db, sub, *a, **k)

    monkeypatch.setattr(cutoff, "_materialize_one", flaky)
    res = await _run(client, ah, "2039-03-01")
    assert res["failed"] == 1
    run_status = (await _q("SELECT status FROM delivery_schedule_runs WHERE id=:i", i=res["schedule_run_id"]))[0][0]
    assert run_status == "partial"


async def test_cancelled_run_is_marked_failed(client, monkeypatch):
    from app.core.db import SessionLocal
    from app.services import cutoff

    async def cancelled(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(cutoff, "_load_ctx", cancelled)
    async with SessionLocal() as db:
        with pytest.raises(asyncio.CancelledError):
            await cutoff.run_cutoff(db, dt.date(2039, 3, 2))
    [(status,)] = await _q(
        "SELECT status FROM delivery_schedule_runs WHERE for_delivery_date='2039-03-02' ORDER BY id DESC LIMIT 1")
    assert status == "failed"


async def test_scheduled_cutoff_retries_on_partial(monkeypatch):
    from arq import Retry

    from app import worker

    async def partial(db, d):
        return {"delivery_date": d, "orders_generated": 3, "failed": 2}

    monkeypatch.setattr(worker, "run_cutoff", partial)
    with pytest.raises(Retry):
        await worker.scheduled_cutoff({"job_try": 1})
    assert (await worker.scheduled_cutoff({"job_try": 4}))["failed"] == 2  # last try returns


# ---------- C9: admin cutoff refuses today/past unless forced ----------
async def test_admin_cutoff_refuses_today_and_past(client):
    from app.services.subscription import today_ist

    ah = auth(await login(client, "9999900001"))
    today = today_ist().isoformat()
    assert (await client.post(f"/api/v1/admin/cutoff/run?date={today}", json={}, headers=ah)).status_code == 422
    assert (await client.post("/api/v1/admin/cutoff/run?date=2020-01-01", json={}, headers=ah)).status_code == 422


# ---------- C11: concurrent subscription create ----------
async def test_concurrent_create_subscription_same_shop(client):
    ah = auth(await login(client, "9999900001"))
    shop = (await client.post("/api/v1/shops", json={"name": "C11 Shop"}, headers=ah)).json()["id"]
    r1, r2 = await asyncio.gather(
        client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=ah),
        client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=ah),
    )
    assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)
    assert r1.json()["id"] == r2.json()["id"]


# ---------- C12: prepaid gate reserves other open unbilled orders ----------
async def test_prepaid_gate_counts_other_open_orders(client):
    ah = auth(await login(client, "9999900001"))
    cust = await login(client, "9000000903")
    sid = (await client.post("/api/v1/subscriptions", json={"home_address": "5 Rd"},
                             headers=auth(cust))).json()["id"]
    milk = await _milk(client, cust)
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": milk, "default_qty": 2, "frequency": "daily"}], headers=auth(cust))
    pay = (await client.post("/api/v1/payments/create", json={"purpose": "wallet_topup", "amount": 100},
                             headers=auth(cust))).json()
    await client.post("/api/v1/payments/verify", json={
        "gateway_order_id": pay["gateway_order_id"], "gateway_payment_id": "p", "signature": "dev"},
        headers=auth(cust))
    shop = (await _q("SELECT shop_id FROM subscriptions WHERE id=:i", i=sid))[0][0]
    await _run(client, ah, "2039-04-01")  # 54 of the 100 reserved
    assert len(await _orders_for(shop, "2039-04-01")) == 1
    await _run(client, ah, "2039-04-02")  # 100 - 54 = 46 < 54 -> gated
    assert await _orders_for(shop, "2039-04-02") == []
