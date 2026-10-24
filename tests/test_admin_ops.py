"""Admin read models: wallet / credit ledgers and delivery fulfilment (POD + cash)."""
import uuid

import sqlalchemy as sa
from conftest import auth, idem, login

from app.core.db import engine


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


async def _stop(client, h, date, *, qty=5, price=10):
    tag = uuid.uuid4().hex[:8]
    pid = (await client.post("/api/v1/products", json={"sku": f"AOP-{tag}", "name": "aop"}, headers=h)).json()["id"]
    tid = (
        await client.post("/api/v1/admin/price-tiers", json={"code": f"AOP{tag}", "name": "t"}, headers=h)
    ).json()["id"]
    await client.post(
        "/api/v1/admin/product-prices",
        json={"product_id": pid, "price_tier_id": tid, "unit_price": price, "valid_from": "2025-01-01"},
        headers=h,
    )
    shop = (await client.post("/api/v1/shops", json={"name": f"AOP {tag}"}, headers=h)).json()["id"]
    await client.post(f"/api/v1/admin/shops/{shop}/tier", json={"price_tier_id": tid}, headers=h)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=h)).json()["id"]
    await client.put(
        f"/api/v1/subscriptions/{sid}/items",
        json=[{"product_id": pid, "default_qty": qty, "frequency": "daily"}],
        headers=h,
    )
    await client.post(
        "/api/v1/admin/stock/batches",
        json={"product_id": pid, "batch_no": f"B{tag}", "qty": 100, "expiry_date": "2045-01-01"},
        headers=idem(h),
    )
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=h)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=h)).json()
    return pid, next(o for o in orders if o["shop_id"] == shop)


async def test_fulfillment_shows_pod_cash_and_returns(client):
    h = auth(await login(client, "9999900001"))
    pid, order = await _stop(client, h, "2042-05-05")
    oid = order["id"]

    before = (await client.get(f"/api/v1/admin/delivery-orders/{oid}/fulfillment", headers=h)).json()
    assert before["order"]["id"] == oid and before["order"]["status"] in ("generated", "frozen")
    assert before["pod"] == [] and before["cash"] == [] and before["cash_total"] == 0

    ok = await client.post(
        f"/api/v1/delivery/orders/{oid}/deliver",
        json={
            "items": [{"product_id": pid, "qty_delivered": 4}],
            "pod": {"recipient_name": "Ravi", "photo_url": "https://cdn.example/pod.jpg"},
            "returns": [{"product_id": pid, "qty_returned": 1, "reason": "damaged"}],
            "cash_collected": 30,
            "cash_method": "upi",
            "cash_reference": "UPI123",
        },
        headers=h,
    )
    assert ok.status_code == 200, ok.text

    f = (await client.get(f"/api/v1/admin/delivery-orders/{oid}/fulfillment", headers=h)).json()
    assert f["order"]["status"] == "partially_delivered"  # 4 of 5 delivered
    assert len(f["pod"]) == 1
    assert f["pod"][0]["recipient_name"] == "Ravi"
    assert f["pod"][0]["photo_url"] == "https://cdn.example/pod.jpg"
    assert [(c["amount_collected"], c["method"], c["reference"]) for c in f["cash"]] == [(30.0, "upi", "UPI123")]
    assert f["cash_total"] == 30.0
    assert [(r["product_id"], r["qty_returned"], r["reason"]) for r in f["returns"]] == [(pid, 1.0, "damaged")]


async def test_fulfillment_signs_private_pod_media(client):
    h = auth(await login(client, "9999900001"))
    pid, order = await _stop(client, h, "2042-05-06")
    oid = order["id"]
    await client.post(
        f"/api/v1/delivery/orders/{oid}/deliver",
        json={"items": [{"product_id": pid, "qty_delivered": 5}], "pod": {"signature_url": "media:private/pod/1/x.png"}},
        headers=h,
    )
    f = (await client.get(f"/api/v1/admin/delivery-orders/{oid}/fulfillment", headers=h)).json()
    url = f["pod"][0]["signature_url"]
    assert not url.startswith("media:") and "sig=" in url  # resolved to a signed, expiring link


async def test_fulfillment_404_and_admin_only(client):
    h = auth(await login(client, "9999900001"))
    assert (await client.get("/api/v1/admin/delivery-orders/99999999/fulfillment", headers=h)).status_code == 404
    driver = auth(await login(client, "9999900003"))
    assert (await client.get("/api/v1/admin/delivery-orders/1/fulfillment", headers=driver)).status_code == 403


async def test_wallet_transactions_and_credit_ledger(client):
    h = auth(await login(client, "9999900001"))
    # The seeded customer's account.
    acct = (await _q("SELECT ca.id FROM customer_accounts ca JOIN users u ON u.id=ca.owner_user_id WHERE u.phone='9876543210'"))
    if acct:
        account_id = acct[0][0]
    else:
        account_id = (await client.get("/api/v1/admin/accounts", headers=h)).json()[0]["id"]

    r1 = await client.post("/api/v1/billing/admin/topup", json={"account_id": account_id, "amount": 120}, headers=idem(h))
    assert r1.status_code == 200, r1.text
    r2 = await client.post(
        f"/api/v1/admin/accounts/{account_id}/wallet/adjust",
        json={"amount": -20, "reason": "goodwill"},
        headers=idem(h),
    )
    assert r2.status_code == 200, r2.text

    tx = await client.get(f"/api/v1/admin/accounts/{account_id}/wallet/transactions", headers=h)
    assert tx.status_code == 200
    rows = tx.json()
    assert int(tx.headers["x-total-count"]) >= 2
    # Newest first, with running balances.
    assert rows[0]["amount"] == -20.0 and rows[1]["amount"] == 120.0
    assert rows[0]["balance_after"] == rows[1]["balance_after"] - 20.0
    assert rows[1]["entry_type"] == "topup"

    one = await client.get(f"/api/v1/admin/accounts/{account_id}/wallet/transactions?limit=1", headers=h)
    assert len(one.json()) == 1 and one.headers["x-total-count"] == tx.headers["x-total-count"]

    cl = await client.get(f"/api/v1/admin/accounts/{account_id}/credit/ledger", headers=h)
    assert cl.status_code == 200 and isinstance(cl.json(), list)
    assert int(cl.headers["x-total-count"]) == len(cl.json())


async def test_ledgers_404_for_unknown_account(client):
    h = auth(await login(client, "9999900001"))
    assert (await client.get("/api/v1/admin/accounts/99999999/wallet/transactions", headers=h)).status_code == 404
    assert (await client.get("/api/v1/admin/accounts/99999999/credit/ledger", headers=h)).status_code == 404
    ch = auth(await login(client, "9876543210"))
    assert (await client.get("/api/v1/admin/accounts/1/credit/ledger", headers=ch)).status_code == 403


# ---------- single-record admin reads (support ticket, CMS) ----------
async def test_admin_get_ticket_any_owner(client):
    ch = auth(await login(client, "9876543210"))
    t = await client.post(
        "/api/v1/support/tickets",
        json={"subject": "Milk was sour", "category": "product", "body": "The 500ml pouch was sour."},
        headers=ch,
    )
    assert t.status_code == 200, t.text
    tid = t.json()["id"]

    h = auth(await login(client, "9999900001"))
    got = await client.get(f"/api/v1/admin/support/tickets/{tid}", headers=h)
    assert got.status_code == 200
    body = got.json()
    assert body["subject"] == "Milk was sour"
    assert [m["body"] for m in body["messages"]] == ["The 500ml pouch was sour."]
    assert (await client.get("/api/v1/admin/support/tickets/99999999", headers=h)).status_code == 404
    assert (await client.get(f"/api/v1/admin/support/tickets/{tid}", headers=ch)).status_code == 403


async def test_admin_get_cms_records_including_drafts(client):
    h = auth(await login(client, "9999900001"))
    tag = uuid.uuid4().hex[:8]
    art = await client.post(
        "/api/v1/admin/content/articles",
        json={"slug": f"draft-{tag}", "title": "Draft", "body_html": "<p>x</p>"},
        headers=h,
    )
    aid = art.json()["id"]
    got = await client.get(f"/api/v1/admin/content/articles/{aid}", headers=h)
    assert got.status_code == 200 and got.json()["status"] == "draft"

    pg = await client.post(
        "/api/v1/admin/content/pages",
        json={"slug": f"pg-{tag}", "title": "About", "body_html": "<p>about</p>"},
        headers=h,
    )
    pid = pg.json()["id"]
    assert (await client.get(f"/api/v1/admin/content/pages/{pid}", headers=h)).json()["slug"] == f"pg-{tag}"

    bn = await client.post(
        "/api/v1/admin/content/banners",
        json={"title": "Monsoon offer", "image_url": "https://cdn.example/b.jpg"},
        headers=h,
    )
    bid = bn.json()["id"]
    assert (await client.get(f"/api/v1/admin/content/banners/{bid}", headers=h)).json()["title"] == "Monsoon offer"

    for path in ("articles", "pages", "banners"):
        assert (await client.get(f"/api/v1/admin/content/{path}/99999999", headers=h)).status_code == 404
    ch = auth(await login(client, "9876543210"))
    assert (await client.get(f"/api/v1/admin/content/articles/{aid}", headers=ch)).status_code == 403
