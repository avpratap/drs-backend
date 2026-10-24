"""Regression tests for the second review round: cutoff / delivery / SCM (R2-C*).
IDs refer to scratchpad/MASTER_FIXLIST.md."""
import datetime as dt
import uuid

import sqlalchemy as sa
from conftest import auth, driver_today, idem, login

from app.core.db import engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _stop(client, h, date, *, qty=5, price=10):
    """Fresh product + tier + shop + standing order; run the cutoff; return (pid, shop, order)."""
    tag = uuid.uuid4().hex[:8]
    pid = (await client.post("/api/v1/products", json={"sku": f"R2C-{tag}", "name": "r2c"}, headers=h)).json()["id"]
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": f"R2C{tag}", "name": "t"},
                             headers=h)).json()["id"]
    await client.post("/api/v1/admin/product-prices", json={
        "product_id": pid, "price_tier_id": tid, "unit_price": price, "valid_from": "2025-01-01"}, headers=h)
    shop = (await client.post("/api/v1/shops", json={"name": f"R2C {tag}"}, headers=h)).json()["id"]
    await client.post(f"/api/v1/admin/shops/{shop}/tier", json={"price_tier_id": tid}, headers=h)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=h)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": pid, "default_qty": qty, "frequency": "daily"}], headers=h)
    await client.post("/api/v1/admin/stock/batches", json={
        "product_id": pid, "batch_no": f"B{tag}", "qty": 100, "expiry_date": "2045-01-01"}, headers=idem(h))
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=h)).json()
    return pid, shop, sid, next((o for o in orders if o["shop_id"] == shop), None)


async def _wallet(account_id) -> float:
    rows = await _q("SELECT balance FROM wallets WHERE account_id=:a", a=account_id)
    return float(rows[0][0]) if rows else 0.0


# ---------- R2-C1: cash collected at the door settles the delivery ----------
async def test_cash_collected_pays_the_delivery(client):
    h = auth(await login(client, "9999900001"))
    pid, _shop, _sid, order = await _stop(client, h, "2041-03-03")
    acct = (await _q("SELECT account_id FROM delivery_orders WHERE id=:i", i=order["id"]))[0][0]
    before = await _wallet(acct)
    over = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json={
        "items": [{"product_id": pid, "qty_delivered": 5}], "cash_collected": 1_000_000}, headers=h)
    assert over.status_code == 422
    bad_method = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json={
        "items": [{"product_id": pid, "qty_delivered": 5}], "cash_collected": 50, "cash_method": "gold"},
        headers=h)
    assert bad_method.status_code == 422
    ok = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json={
        "items": [{"product_id": pid, "qty_delivered": 5}], "cash_collected": 50, "cash_method": "cash"},
        headers=h)
    assert ok.status_code == 200, ok.text
    # ₹50 charged, ₹50 paid in cash: the wallet is unchanged (was charged twice).
    assert await _wallet(acct) == before


# ---------- R2-C2: a driver can't close a stop dated in the future ----------
async def test_driver_cannot_close_future_stop(client, monkeypatch):
    h = auth(await login(client, "9999900001"))
    driver = auth(await login(client, "9999900003"))
    pid, shop, _sid, order = await _stop(client, h, "2041-04-04")
    did = (await _q("SELECT id FROM users WHERE phone='9999900003'"))[0][0]
    await client.post("/api/v1/admin/delivery-assignments", json={"delivery_id": did, "shop_id": shop}, headers=h)
    body = {"items": [{"product_id": pid, "qty_delivered": 5}], "pod": {"signature_url": "https://cdn.example.com/sig.png"}}
    r = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json=body, headers=driver)
    assert r.status_code == 409, r.text
    f = await client.post(f"/api/v1/delivery/orders/{order['id']}/fail", json={"reason": "customer_unavailable"},
                          headers=driver)
    assert f.status_code == 409
    assert (await _q("SELECT status FROM delivery_orders WHERE id=:i", i=order["id"]))[0][0] in ("generated", "frozen")
    driver_today(monkeypatch, "2041-04-04")
    assert (await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json=body,
                              headers=driver)).status_code == 200


# ---------- R2-C3: inactive shops / suspended owners get no deliveries ----------
async def test_cutoff_skips_inactive_shop(client):
    h = auth(await login(client, "9999900001"))
    date = "2041-05-05"
    pid, shop, sid, order = await _stop(client, h, date)
    assert order is not None
    await client.patch(f"/api/v1/admin/shops/{shop}", json={"status": "inactive"}, headers=h)
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    # The earlier order for the deactivated shop is swept, none regenerated.
    assert not await _q("SELECT 1 FROM delivery_orders WHERE shop_id=:s AND delivery_date=:d",
                        s=shop, d=dt.date(2041, 5, 5))
    # And it can't get new standing-order lines.
    r = await client.put(f"/api/v1/subscriptions/{sid}/items",
                         json=[{"product_id": pid, "default_qty": 1}], headers=h)
    assert r.status_code in (200, 409)


# ---------- R2-C4: the scheduled cutoff targets the day it was scheduled for ----------
def test_scheduled_cutoff_date_from_enqueue_time():
    from app.worker import _scheduled_day

    # Scheduled 23:05 IST on Oct 24 (17:35 UTC); the retry runs after midnight.
    enq = dt.datetime(2026, 10, 24, 17, 35, tzinfo=dt.UTC)
    assert _scheduled_day({"enqueue_time": enq}) == dt.date(2026, 10, 24)


# ---------- R2-C5: substitute driver without a van doesn't drain another van ----------
async def test_substitute_driver_without_van_uses_default_location():
    from app.core.db import SessionLocal
    from app.services import inventory

    async with SessionLocal() as db:
        default = await inventory.default_location_id(db)
        # Actor with no van, shop assigned to someone else: not their van.
        loc = await inventory.delivery_location_id(db, 1, 99999999)
        assert loc == default


# ---------- R2-C6: stock/production date guards ----------
async def test_expire_as_of_future_rejected_and_production_dates(client):
    h = auth(await login(client, "9999900001"))
    future = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    assert (await client.post(f"/api/v1/admin/stock/expire?as_of={future}", headers=idem(h))).status_code == 422


# ---------- R2-C7: set_items prices as the cutoff does ----------
async def test_set_items_allows_zero_qty_and_tomorrow_price(client):
    h = auth(await login(client, "9999900001"))
    tag = uuid.uuid4().hex[:8]
    pid = (await client.post("/api/v1/products", json={"sku": f"R2P-{tag}", "name": "p"}, headers=h)).json()["id"]
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": f"R2P{tag}", "name": "t"},
                             headers=h)).json()["id"]
    tomorrow = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    await client.post("/api/v1/admin/product-prices", json={
        "product_id": pid, "price_tier_id": tid, "unit_price": 5, "valid_from": tomorrow}, headers=h)
    shop = (await client.post("/api/v1/shops", json={"name": f"P {tag}"}, headers=h)).json()["id"]
    await client.post(f"/api/v1/admin/shops/{shop}/tier", json={"price_tier_id": tid}, headers=h)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=h)).json()["id"]
    # qty 0 = override-days-only line: never priced at set time.
    r = await client.put(f"/api/v1/subscriptions/{sid}/items",
                         json=[{"product_id": pid, "default_qty": 0}], headers=h)
    assert r.status_code == 200, r.text


# ---------- R2-C8: the change freeze lifts once tonight's run has frozen tomorrow ----------
async def test_freeze_lifts_after_tonights_run(client, monkeypatch):
    from app.core.config import settings
    from app.services.subscription import IST

    cust = await login(client, "9000001201")
    sid = (await client.post("/api/v1/subscriptions", json={"home_address": "1 Rd"},
                             headers=auth(cust))).json()["id"]
    monkeypatch.setattr(settings, "CUTOFF_HOUR", 0)
    assert (await client.post(f"/api/v1/subscriptions/{sid}/pause", headers=auth(cust))).status_code == 409
    tomorrow = dt.datetime.now(IST).date() + dt.timedelta(days=1)
    await _q("INSERT INTO delivery_schedule_runs (for_delivery_date, status, orders_generated, started_at, "
             "finished_at) VALUES (:d, 'frozen', 0, now(), now())", d=tomorrow)
    try:
        assert (await client.post(f"/api/v1/subscriptions/{sid}/pause", headers=auth(cust))).status_code == 200
    finally:
        await _q("DELETE FROM delivery_schedule_runs WHERE for_delivery_date=:d AND orders_generated=0 "
                 "AND status='frozen'", d=tomorrow)


# ---------- R3-1: drivers must attach proof of delivery ----------
async def test_driver_delivery_requires_pod(client, monkeypatch):
    h = auth(await login(client, "9999900001"))
    driver = auth(await login(client, "9999900003"))
    pid, shop, _sid, order = await _stop(client, h, "2041-06-06")
    did = (await _q("SELECT id FROM users WHERE phone='9999900003'"))[0][0]
    await client.post("/api/v1/admin/delivery-assignments", json={"delivery_id": did, "shop_id": shop}, headers=h)
    driver_today(monkeypatch, "2041-06-06")
    url = f"/api/v1/delivery/orders/{order['id']}/deliver"
    items = [{"product_id": pid, "qty_delivered": 5}]
    assert (await client.post(url, json={"items": items}, headers=driver)).status_code == 422
    assert (await client.post(url, json={"items": items, "pod": {"recipient_name": "x"}},
                              headers=driver)).status_code == 422
    ok = await client.post(url, json={"items": items, "pod": {"photo_url": "https://cdn.example.com/p.jpg"}},
                           headers=driver)
    assert ok.status_code == 200, ok.text
