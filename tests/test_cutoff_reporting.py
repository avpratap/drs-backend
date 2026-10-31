"""Cutoff bookkeeping: a subscription whose savepoint rolls back leaves no trace in the
run's tallies or the prepaid wallet snapshot, and the low-balance nudge reports the
customer's REAL wallet balance (not the planning figure net of reservations)."""
from conftest import auth, login
from sqlalchemy import select
from test_wallet_low import ORDER, _b2b_prepaid

from app.core.db import SessionLocal
from app.models.notification import Notification


async def test_rolled_back_sub_does_not_consume_the_wallet_snapshot(client, monkeypatch):
    """Wallet covers ONE order; two shops subscribe. The first sub fails at the very end
    (after the snapshot was decremented): its reservation must be returned, so the second
    shop is still scheduled, and the run counts exactly one order."""
    from app.services import cutoff

    ah = auth(await login(client, "9999900001"))
    h, acct, shop1 = await _b2b_prepaid(client, ah, "2035-01-01", fund=ORDER)
    shop2 = (await client.post("/api/v1/shops", json={"name": f"second {acct}"}, headers=h)).json()["id"]
    sid2 = (await client.post("/api/v1/subscriptions", json={"shop_id": shop2}, headers=h)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid2}/items",
                     json=[{"product_id": 1, "default_qty": 5, "frequency": "daily"}], headers=h)

    real_emit = cutoff.notify.emit_event
    state = {"failed": False}

    async def flaky_emit(db, event, user, payload, **kw):
        if event == "order.generated" and not state["failed"] and kw.get("account_id") == acct:
            state["failed"] = True
            raise RuntimeError("boom after the order was built")
        return await real_emit(db, event, user, payload, **kw)

    monkeypatch.setattr(cutoff.notify, "emit_event", flaky_emit)
    r = await client.post("/api/v1/admin/cutoff/run?date=2035-01-01", json={}, headers=ah)
    assert r.status_code == 200, r.text
    assert state["failed"] and r.json()["failed"] >= 1
    orders = [o for o in (await client.get("/api/v1/delivery/orders?date=2035-01-01",
                                           headers=ah)).json() if o["shop_id"] in (shop1, shop2)]
    assert len(orders) == 1  # the surviving shop was scheduled with the returned funds


async def test_low_balance_nudge_reports_the_real_balance(client):
    """₹200 in the wallet, ₹135 already reserved by an open order for another date: the
    next order is skipped, and the message shows balance 200 (not the planning 65)."""
    ah = auth(await login(client, "9999900001"))
    h, _acct, shop = await _b2b_prepaid(client, ah, "2035-02-02", fund=200)
    await client.post("/api/v1/admin/cutoff/run?date=2035-02-02", json={}, headers=ah)  # reserves 135
    r = await client.post("/api/v1/admin/cutoff/run?date=2035-02-03", json={}, headers=ah)
    assert r.status_code == 200
    day2 = [o for o in (await client.get("/api/v1/delivery/orders?date=2035-02-03",
                                         headers=ah)).json() if o["shop_id"] == shop]
    assert day2 == []  # skipped: 200 - 135 reserved < 135
    uid = (await client.get("/api/v1/auth/me", headers=h)).json()["id"]
    async with SessionLocal() as db:
        payloads = [n.payload for n in (await db.execute(select(Notification).where(
            Notification.user_id == uid, Notification.event_type == "wallet.low"))).scalars()]
    assert payloads and all(p["balance"] == 200.0 for p in payloads)
    assert all(p["reserved"] == ORDER and p["needed"] == ORDER for p in payloads)
    assert (await client.get("/api/v1/billing/me", headers=h)).json()["wallet_balance"] == 200.0
