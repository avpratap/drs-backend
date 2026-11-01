"""Backend guarantees the web client relies on: no dev OTP in prod, OTP request + verify
throttled per phone AND per IP, and per-shop scoping of invoices / credit statement."""
import datetime as dt
import uuid

from conftest import auth, login

from app.core.db import SessionLocal
from app.services import otp as otp_service

# ---------- (1) dev_otp never leaves a prod server ----------


async def test_dev_otp_only_outside_prod(client, monkeypatch):
    from app.core.config import settings

    r = await client.post("/api/v1/auth/otp/request", json={"phone": "9000002001"})
    assert r.status_code == 200 and len(r.json()["dev_otp"]) == 6  # dev: shown for testing

    monkeypatch.setattr(settings, "ENV", "prod")
    r = await client.post("/api/v1/auth/otp/request", json={"phone": "9000002002"})
    assert r.status_code == 200
    assert "dev_otp" not in r.json()  # absent, not null: a presence check can't trip


async def test_dev_otp_absent_once_delivered(client, monkeypatch):
    async def _delivered(phone, otp):
        return True

    # Simulate a provider (Twilio/MSG91) accepting the code: dev_otp must then be withheld.
    monkeypatch.setattr(otp_service, "deliver_otp", _delivered)
    r = await client.post("/api/v1/auth/otp/request", json={"phone": "9000002003"})
    assert r.status_code == 200 and "dev_otp" not in r.json()


# ---------- (2) per-phone AND per-IP limits on request + verify ----------


async def test_otp_request_limited_per_phone_and_per_ip(client, monkeypatch):
    url = "/api/v1/auth/otp/request"
    assert (await client.post(url, json={"phone": "9000002010"})).status_code == 200
    assert (await client.post(url, json={"phone": "9000002010"})).status_code == 429  # cooldown

    monkeypatch.setattr(otp_service, "MAX_SENDS_PER_IP_HOUR", 3)
    codes = [(await client.post(url, json={"phone": f"900000202{i}"})).status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]  # 1 earlier + 2 more = cap of 3 for this IP


async def test_otp_verify_limited_per_phone(client):
    phone = "9000002030"
    otp = (await client.post("/api/v1/auth/otp/request", json={"phone": phone})).json()["dev_otp"]
    wrong = f"{(int(otp) + 1) % 1_000_000:06d}"
    for _ in range(otp_service.MAX_VERIFY_ATTEMPTS):
        r = await client.post("/api/v1/auth/otp/verify", json={"phone": phone, "otp": wrong})
        assert r.status_code == 401
    # The code is burned after the attempt cap: even the right one no longer works.
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": phone, "otp": otp})
    assert r.status_code == 401


async def test_otp_verify_ip_failures_throttle_but_never_block_a_correct_code(client, monkeypatch):
    """B1: on a shared CGNAT/office IP past the failure threshold, a user with the RIGHT
    code still logs in (only slowed); the hard limit stays per phone."""
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(otp_service, "VERIFY_THROTTLE_AFTER_FAILS", 3)
    monkeypatch.setattr(otp_service.asyncio, "sleep", fake_sleep)
    url = "/api/v1/auth/otp/verify"
    # A correct login doesn't count as a failure.
    assert (await login(client, "9000002040")) is not None
    otp = (await client.post("/api/v1/auth/otp/request", json={"phone": "9000002041"})).json()["dev_otp"]
    wrong = "000000" if otp != "000000" else "111111"
    for phone in ("9000002042", "9000002043", "9000002044"):  # someone guessing on the NAT
        assert (await client.post(url, json={"phone": phone, "otp": wrong})).status_code == 401
    assert slept == []
    # Another user behind the same IP, correct code: throttled, NOT blocked.
    r = await client.post(url, json={"phone": "9000002041", "otp": otp})
    assert r.status_code == 200 and slept == [otp_service.VERIFY_THROTTLE_DELAY_S]
    # Wrong guesses past the threshold are slowed too (and still just 401).
    assert (await client.post(url, json={"phone": "9000002042", "otp": wrong})).status_code == 401
    assert len(slept) == 2


async def test_otp_verify_ip_ceiling_is_a_hard_429(client, monkeypatch):
    monkeypatch.setattr(otp_service, "VERIFY_FAILS_PER_IP_CEILING", 2)
    monkeypatch.setattr(otp_service, "VERIFY_THROTTLE_AFTER_FAILS", 100)
    url = "/api/v1/auth/otp/verify"
    for phone in ("9000002045", "9000002046"):
        assert (await client.post(url, json={"phone": phone, "otp": "123456"})).status_code == 401
    assert (await client.post(url, json={"phone": "9000002047", "otp": "123456"})).status_code == 429


# ---------- (3) ?shop_id= on /invoices and /billing/credit-statement ----------


async def _b2b_with_two_shops(client):
    """A B2B credit customer with shops A and B: per shop a delivery order + invoice + its
    credit charge, cash collected at A's door, plus an account-level settlement."""
    from app.models.delivery import DeliveryOrder
    from app.models.fulfillment import CashCollection
    from app.models.invoice import Invoice
    from app.models.shop import Shop
    from app.services import billing

    admin = auth(await login(client, "9999900001"))
    h = auth(await login(client, f"90000{uuid.uuid4().int % 10**5:05d}"))
    await client.get("/api/v1/loyalty", headers=h)  # ensure the account exists
    acct = (await client.get("/api/v1/billing/me", headers=h)).json()["account_id"]
    await client.patch(f"/api/v1/admin/accounts/{acct}/type",
                       json={"account_type": "b2b", "billing_mode": "postpaid_credit"}, headers=admin)
    tag = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        shops = [Shop(account_id=acct, name=f"Shop {s} {tag}", status="active") for s in "AB"]
        db.add_all(shops)
        await db.flush()
        out = {}
        for i, shop in enumerate(shops):
            do = DeliveryOrder(delivery_date=dt.date(2001, 1, 1), account_id=acct, shop_id=shop.id,
                               status="delivered", total=100 * (i + 1))
            db.add(do)
            await db.flush()
            inv = Invoice(invoice_no=f"T-{tag}-{i}", fy="2000-01", seq_no=uuid.uuid4().int % 10**9,
                          account_id=acct, delivery_order_id=do.id, invoice_date=dt.date(2001, 1, 1),
                          total=100 * (i + 1))
            db.add(inv)
            await db.flush()
            await billing.credit_entry(db, acct, 100 * (i + 1), "charge", "delivery_order", do.id)
            out[shop.id] = (do, inv)
        cc = CashCollection(delivery_order_id=out[shops[0].id][0].id, amount_collected=40,
                            method="cash", collected_by=None)
        db.add(cc)
        await db.flush()
        await billing.credit_entry(db, acct, -40, "payment", "cash_collection", cc.id)
        await billing.credit_entry(db, acct, -10, "payment", "settlement", None)
        await db.commit()
    return h, acct, shops[0].id, shops[1].id, out


async def test_invoices_filter_by_shop(client):
    h, _acct, a, b, out = await _b2b_with_two_shops(client)
    every = {i["invoice_no"] for i in (await client.get("/api/v1/invoices", headers=h)).json()}
    assert {out[a][1].invoice_no, out[b][1].invoice_no} <= every
    only_a = (await client.get(f"/api/v1/invoices?shop_id={a}", headers=h)).json()
    assert [i["invoice_no"] for i in only_a] == [out[a][1].invoice_no]
    only_b = (await client.get(f"/api/v1/invoices?shop_id={b}", headers=h)).json()
    assert [i["invoice_no"] for i in only_b] == [out[b][1].invoice_no]
    # Someone else's shop: 404, not an empty list that confirms it exists.
    other = auth(await login(client, "9000002099"))
    assert (await client.get(f"/api/v1/invoices?shop_id={a}", headers=other)).status_code == 404
    # Admins can scope any shop.
    admin = auth(await login(client, "9999900001"))
    r = (await client.get(f"/api/v1/invoices?shop_id={b}", headers=admin)).json()
    assert [i["invoice_no"] for i in r] == [out[b][1].invoice_no]


async def test_credit_statement_filter_by_shop(client):
    h, _acct, a, b, _out = await _b2b_with_two_shops(client)
    url = "/api/v1/billing/credit-statement"
    full = await client.get(url, headers=h)
    assert full.headers["x-total-count"] == "4"
    assert [x["amount"] for x in full.json()] == [-10.0, -40.0, 200.0, 100.0]

    ra = await client.get(f"{url}?shop_id={a}", headers=h)
    assert ra.status_code == 200 and ra.headers["x-total-count"] == "2"
    # A's charge + the cash taken at A's door; balance_after stays account-wide.
    assert [(x["entry_type"], x["amount"], x["balance_after"]) for x in ra.json()] == [
        ("payment", -40.0, 260.0), ("charge", 100.0, 100.0)]
    rb = (await client.get(f"{url}?shop_id={b}", headers=h)).json()
    assert [(x["entry_type"], x["amount"]) for x in rb] == [("charge", 200.0)]
    # The account-level settlement belongs to no shop.
    assert all(x["amount"] != -10.0 for x in ra.json() + rb)

    other = auth(await login(client, "9000002098"))
    assert (await client.get(f"{url}?shop_id={a}", headers=other)).status_code == 404
    assert (await client.get(f"{url}?shop_id=999999999", headers=h)).status_code == 404
