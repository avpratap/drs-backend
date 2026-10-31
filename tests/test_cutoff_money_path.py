"""End-to-end guard on the money + cutoff path, through the API:

subscription -> cutoff materializes delivery_orders (+ ScheduleRun) -> delivery ->
prepaid wallet debit (wallet_transactions) / postpaid credit charge (credit_ledger) ->
invoice; with re-runs of the cutoff and re-delivery never double-charging, and each
ledger replaying exactly to the balance it reports.
"""
import uuid

from conftest import auth, idem, login
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.billing import CreditLedger, Wallet, WalletTransaction
from app.models.delivery import DeliveryOrder, ScheduleRun
from app.models.invoice import Invoice

PRICE = 27.0  # retail unit price of product 1 in the test seed
QTY = 5
ORDER = PRICE * QTY  # 135.0


async def _subscriber(client, ah, *, postpaid: bool = False, fund: float = 0):
    phone = f"8{uuid.uuid4().int % 10**9:09d}"
    await client.post("/api/v1/admin/users", json={"phone": phone, "role": "subscriber"}, headers=ah)
    h = auth(await login(client, phone))
    shop = (await client.post("/api/v1/shops", json={"name": f"Money {phone}"}, headers=h)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=h)).json()["id"]
    r = await client.put(f"/api/v1/subscriptions/{sid}/items",
                         json=[{"product_id": 1, "default_qty": QTY, "frequency": "daily"}], headers=h)
    assert r.status_code == 200, r.text
    acct = (await client.get("/api/v1/billing/me", headers=h)).json()["account_id"]
    if postpaid:
        r = await client.patch(f"/api/v1/admin/accounts/{acct}/type",
                               json={"account_type": "b2b", "billing_mode": "postpaid_credit"},
                               headers=ah)
        assert r.status_code == 200, r.text
    if fund:
        r = await client.post("/api/v1/billing/admin/topup",
                              json={"account_id": acct, "amount": fund}, headers=idem(ah))
        assert r.status_code == 200, r.text
    return h, acct, shop


async def _cutoff(client, ah, date):
    r = await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=ah)
    assert r.status_code == 200, r.text
    return r.json()


async def _orders(shop, date):
    import datetime as dt

    async with SessionLocal() as db:
        return list((await db.execute(select(DeliveryOrder).where(
            DeliveryOrder.shop_id == shop,
            DeliveryOrder.delivery_date == dt.date.fromisoformat(date)))).scalars())


async def _deliver(client, ah, order_id):
    return await client.post(f"/api/v1/delivery/orders/{order_id}/deliver", json={
        "items": [{"product_id": 1, "qty_delivered": QTY}], "returns": []}, headers=ah)


async def _wallet_txns(acct):
    async with SessionLocal() as db:
        wallet = await db.scalar(select(Wallet).where(Wallet.account_id == acct))
        rows = list((await db.execute(select(WalletTransaction).where(
            WalletTransaction.wallet_id == wallet.id).order_by(WalletTransaction.id))).scalars())
        return float(wallet.balance), rows


def _replays(rows, balance):
    """Each row's balance_after = previous + amount, ending at the reported balance."""
    running = 0.0
    for r in rows:
        running = round(running + float(r.amount), 2)
        assert float(r.balance_after) == running, (r.id, r.balance_after, running)
    assert running == round(balance, 2)


async def test_prepaid_cutoff_to_wallet_debit(client):
    ah = auth(await login(client, "9999900001"))
    date = "2038-01-10"
    h, acct, shop = await _subscriber(client, ah, fund=200)

    # Cutoff materializes exactly one order at the subscription's price; money not yet moved.
    res = await _cutoff(client, ah, date)
    assert res["orders_generated"] >= 1 and not res.get("failed")
    [order] = await _orders(shop, date)
    assert order.status == "generated" and float(order.total) == ORDER
    async with SessionLocal() as db:
        run = await db.get(ScheduleRun, res["schedule_run_id"])
        assert run.status == "frozen" and str(run.for_delivery_date) == date
    assert (await client.get("/api/v1/billing/me", headers=h)).json()["wallet_balance"] == 200.0

    # Re-running the cutoff is idempotent: same single order, same total.
    await _cutoff(client, ah, date)
    [again] = await _orders(shop, date)
    assert again.id == order.id and float(again.total) == ORDER

    # Delivery debits the wallet once, referencing the order.
    r = await _deliver(client, ah, order.id)
    assert r.status_code == 200, r.text
    balance, txns = await _wallet_txns(acct)
    assert balance == 200.0 - ORDER
    debits = [t for t in txns if t.txn_type == "debit_order"]
    assert [(float(t.amount), t.ref_type, t.ref_id) for t in debits] == [
        (-ORDER, "delivery_order", order.id)]
    _replays(txns, balance)
    assert (await client.get("/api/v1/billing/me", headers=h)).json()["wallet_balance"] == balance
    # The tax invoice (issued by the admin generate step) bills exactly what was debited,
    # and re-issuing returns the same document.
    gen = f"/api/v1/admin/invoices/generate?delivery_order_id={order.id}"
    r1 = await client.post(gen, headers=ah)
    assert r1.status_code == 200, r1.text
    assert r1.json()["total"] == ORDER
    assert (await client.post(gen, headers=ah)).json()["invoice_no"] == r1.json()["invoice_no"]
    async with SessionLocal() as db:
        invs = list((await db.execute(select(Invoice).where(
            Invoice.delivery_order_id == order.id))).scalars())
        assert len(invs) == 1 and invs[0].account_id == acct

    # Neither a second delivery call nor another cutoff run charges again.
    await _deliver(client, ah, order.id)
    await _cutoff(client, ah, date)
    balance2, txns2 = await _wallet_txns(acct)
    assert balance2 == balance and len(txns2) == len(txns)
    [delivered] = await _orders(shop, date)
    assert delivered.status == "delivered"


async def test_unfunded_prepaid_is_not_materialized_or_charged(client):
    ah = auth(await login(client, "9999900001"))
    date = "2038-01-11"
    _h, acct, shop = await _subscriber(client, ah, fund=ORDER - 1)
    await _cutoff(client, ah, date)
    assert await _orders(shop, date) == []
    balance, txns = await _wallet_txns(acct)
    assert balance == ORDER - 1 and not [t for t in txns if t.txn_type == "debit_order"]


async def test_postpaid_cutoff_to_credit_ledger_charge(client):
    ah = auth(await login(client, "9999900001"))
    date = "2038-01-12"
    h, acct, shop = await _subscriber(client, ah, postpaid=True)
    await _cutoff(client, ah, date)
    [order] = await _orders(shop, date)
    assert float(order.total) == ORDER
    assert (await _deliver(client, ah, order.id)).status_code == 200
    await _deliver(client, ah, order.id)  # repeat: no second charge
    async with SessionLocal() as db:
        rows = list((await db.execute(select(CreditLedger).where(
            CreditLedger.account_id == acct).order_by(CreditLedger.id))).scalars())
    assert [(r.entry_type, float(r.amount), r.ref_type, r.ref_id) for r in rows] == [
        ("charge", ORDER, "delivery_order", order.id)]
    _replays(rows, float(rows[-1].balance_after))
    statement = (await client.get("/api/v1/billing/credit-statement", headers=h)).json()
    assert [x["amount"] for x in statement] == [ORDER]
    _balance, txns = await _wallet_txns(acct)
    assert not [t for t in txns if t.txn_type == "debit_order"]  # postpaid: wallet untouched
