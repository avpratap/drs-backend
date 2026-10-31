"""Prepaid wallets (retail AND B2B) are never billed into the negative: the cutoff gate
skips an unfunded subscription, and billing re-checks under the wallet lock at delivery
time, deferring the stop (409 + wallet.low) when funds no longer cover it."""
import uuid

from conftest import auth, idem, login
from sqlalchemy import func, select

from app.core.db import SessionLocal

PRICE = 27.0  # RETAIL price of product 1 (MILK-1L) in the test seed
QTY = 5
ORDER = PRICE * QTY  # 135


async def _b2b_prepaid(client, ah, date: str, fund: float):
    """A fresh B2B subscriber (prepaid wallet, the default) with one shop subscribing
    QTY x product 1 daily, wallet funded with `fund`. Returns (headers, account, shop)."""
    phone = f"8{uuid.uuid4().int % 10**9:09d}"
    await client.post("/api/v1/admin/users", json={"phone": phone, "role": "subscriber"}, headers=ah)
    h = auth(await login(client, phone))
    shop = (await client.post("/api/v1/shops", json={"name": f"Prepaid {phone}"}, headers=h)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=h)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": 1, "default_qty": QTY, "frequency": "daily"}], headers=h)
    acct = (await client.get("/api/v1/billing/me", headers=h)).json()["account_id"]
    if fund:
        r = await client.post("/api/v1/billing/admin/topup",
                              json={"account_id": acct, "amount": fund}, headers=idem(ah))
        assert r.status_code == 200, r.text
    return h, acct, shop


async def _order(client, ah, date, shop):
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=ah)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=ah)).json()
    return next((o for o in orders if o["shop_id"] == shop), None)


async def _deliver(client, ah, order, cash=None):
    body = {"items": [{"product_id": 1, "qty_delivered": QTY}], "returns": []}
    if cash is not None:
        body["cash_collected"] = cash
    return await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json=body, headers=ah)


async def _balance(client, h) -> float:
    return (await client.get("/api/v1/billing/me", headers=h)).json()["wallet_balance"]


async def _low_notes(client, ah, h) -> list:
    uid = (await client.get("/api/v1/auth/me", headers=h)).json()["id"]
    notes = (await client.get(f"/api/v1/admin/notifications?user_id={uid}", headers=ah)).json()
    return [n for n in notes if n["event_type"] == "wallet.low"]


async def test_unfunded_b2b_prepaid_is_skipped_at_cutoff(client):
    """B2B prepaid used to bypass the retail-only funds gate entirely."""
    ah = auth(await login(client, "9999900001"))
    h, _acct, shop = await _b2b_prepaid(client, ah, "2033-05-01", fund=0)
    assert await _order(client, ah, "2033-05-01", shop) is None  # not scheduled
    assert await _low_notes(client, ah, h)  # nudged to top up
    assert await _balance(client, h) == 0.0


async def test_delivery_never_bills_a_prepaid_wallet_negative(client):
    """Funded at cutoff, drained afterwards: the stale cutoff snapshot can't let the
    delivery overdraw — it is deferred (409), nothing is billed or recorded, and the
    driver can complete it by collecting the shortfall as cash."""
    from app.models.inventory import StockMovement

    ah = auth(await login(client, "9999900001"))
    h, acct, shop = await _b2b_prepaid(client, ah, "2033-05-02", fund=200)
    order = await _order(client, ah, "2033-05-02", shop)
    assert order is not None and order["total"] == ORDER
    # Spent elsewhere after the cutoff: 200 -> 50.
    r = await client.post(f"/api/v1/admin/accounts/{acct}/wallet/adjust",
                          json={"amount": -150, "reason": "spent"}, headers=idem(ah))
    assert r.status_code == 200, r.text

    r = await _deliver(client, ah, order)
    assert r.status_code == 409 and "short by Rs 85.00" in r.text
    assert await _balance(client, h) == 50.0  # untouched, never negative
    fresh = next(o for o in (await client.get("/api/v1/delivery/orders?date=2033-05-02",
                                              headers=ah)).json() if o["id"] == order["id"])
    assert fresh["status"] in ("generated", "frozen")  # deferred, still open
    async with SessionLocal() as db:
        moved = await db.scalar(select(func.count()).select_from(StockMovement).where(
            StockMovement.ref_type == "delivery_order", StockMovement.ref_id == order["id"]))
    assert moved == 0
    # Funded at cutoff, so the only wallet.low is the deferral's nudge (one event, fanned
    # out once per channel).
    notes = await _low_notes(client, ah, h)
    assert notes and len({n["channel"] for n in notes}) == len(notes)

    # Collecting the shortfall at the door completes it, ending at exactly 0.
    r = await _deliver(client, ah, order, cash=85)
    assert r.status_code == 200, r.text
    assert await _balance(client, h) == 0.0


async def test_wallet_low_emitted_when_delivery_leaves_little(client):
    """A covered delivery that leaves the wallet below the low threshold still nudges."""
    ah = auth(await login(client, "9999900001"))
    h, _acct, shop = await _b2b_prepaid(client, ah, "2033-05-05", fund=200)
    order = await _order(client, ah, "2033-05-05", shop)
    assert (await _deliver(client, ah, order)).status_code == 200
    assert await _balance(client, h) == 65.0  # 200 - 135, not negative
    assert await _low_notes(client, ah, h)


async def test_bill_delivery_refuses_retail_overdraw_under_lock():
    """Same invariant for a retail prepaid account, at the service layer."""
    import datetime as dt

    import pytest

    from app.models.delivery import DeliveryOrder
    from app.models.shop import CustomerAccount, Shop
    from app.models.user import User
    from app.services import billing

    async with SessionLocal() as db:
        user = User(phone=f"7{uuid.uuid4().int % 10**9:09d}", role="customer", status="active")
        db.add(user)
        await db.flush()
        acct = CustomerAccount(owner_user_id=user.id, account_type="retail",
                               billing_mode="prepaid_wallet")
        db.add(acct)
        await db.flush()
        shop = Shop(account_id=acct.id, name="home", status="active")
        db.add(shop)
        await db.flush()
        order = DeliveryOrder(delivery_date=dt.date(2001, 2, 2), account_id=acct.id,
                              shop_id=shop.id, status="generated")
        db.add(order)
        await db.flush()
        await billing.wallet_adjust(db, acct.id, 30, "topup", "test", None)
        with pytest.raises(billing.InsufficientPrepaidBalance):
            await billing.bill_delivery(db, order, 30.01)
        await billing.bill_delivery(db, order, 30)  # exactly covered: allowed
        wallet = await billing.get_or_create_wallet(db, acct.id)
        assert float(wallet.balance) == 0.0
        await db.rollback()
