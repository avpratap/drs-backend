"""Admin endpoints for screens that had no API: return detail (+ refund visibility),
admin subscriptions, user search, cutoff history/status, shop detail, and unique
location names (incl. the migration that renames existing duplicates)."""
import importlib.util
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from conftest import auth, login
from test_rma import _order

from app.core.db import engine

ADMIN, CUSTOMER, DRIVER = "9999900001", "9876543210", "9999900003"


async def _q(sql: str, **p):
    async with engine.begin() as conn:
        res = await conn.execute(sa.text(sql), p)
        return res.all() if res.returns_rows else None


# ---------- (1) + (7) returns ----------
async def test_admin_return_detail_shows_refund_credit_and_limits(client):
    ch = auth(await login(client, CUSTOMER))
    ah = auth(await login(client, ADMIN))
    oid, pid = await _order(client, ch, qty=3)
    rid = (
        await client.post(
            "/api/v1/returns",
            json={"order_id": oid, "items": [{"product_id": pid, "qty": 2, "reason": "sour"}], "reason": "quality"},
            headers=ch,
        )
    ).json()["id"]

    before = await client.get(f"/api/v1/admin/returns/{rid}", headers=ah)
    assert before.status_code == 200, before.text
    d = before.json()
    assert d["status"] == "requested" and d["order_id"] == oid and d["order_no"]
    assert d["customer_phone"] == CUSTOMER
    assert d["returned_value"] > 0 and d["refundable_headroom"] >= d["returned_value"]
    assert d["refund_credits"] == [] and [i["product_id"] for i in d["items"]] == [pid]
    assert d["created_at"]

    await client.post(f"/api/v1/admin/returns/{rid}/approve", headers=ah)
    amount = round(d["returned_value"] / 2, 2)
    assert (await client.post(f"/api/v1/admin/returns/{rid}/refund", json={"amount": amount}, headers=ah)).status_code == 200

    after = (await client.get(f"/api/v1/admin/returns/{rid}", headers=ah)).json()
    assert after["status"] == "refunded" and after["refund_amount"] == amount
    # The store credit the refund produced is visible on the return.
    assert [c["amount"] for c in after["refund_credits"]] == [amount]
    assert after["refundable_headroom"] == round(d["refundable_headroom"] - amount, 2)


async def test_admin_return_list_filters_and_404(client):
    ch = auth(await login(client, CUSTOMER))
    ah = auth(await login(client, ADMIN))
    oid, pid = await _order(client, ch)
    rid = (
        await client.post("/api/v1/returns", json={"order_id": oid, "items": [{"product_id": pid, "qty": 1}]}, headers=ch)
    ).json()["id"]
    by_order = await client.get(f"/api/v1/admin/returns?order_id={oid}", headers=ah)
    assert [r["id"] for r in by_order.json()] == [rid] and by_order.headers["x-total-count"] == "1"
    requested = (await client.get("/api/v1/admin/returns?status_filter=requested", headers=ah)).json()
    assert rid in [r["id"] for r in requested] and all(r["status"] == "requested" for r in requested)
    assert (await client.get("/api/v1/admin/returns?status_filter=bogus", headers=ah)).status_code == 422
    assert (await client.get("/api/v1/admin/returns/99999999", headers=ah)).status_code == 404
    assert (await client.get(f"/api/v1/admin/returns/{rid}", headers=ch)).status_code == 403


# ---------- (2) subscriptions ----------
async def _shop_with_subscription(client, ah, *, items=True):
    tag = uuid.uuid4().hex[:6]
    shop = (await client.post("/api/v1/shops", json={"name": f"Gaps Shop {tag}", "pincode": "302001"}, headers=ah)).json()
    sub = (await client.post("/api/v1/subscriptions", json={"shop_id": shop["id"]}, headers=ah)).json()
    if items:
        pid = (await client.get("/api/v1/products", headers=ah)).json()[0]["id"]
        r = await client.put(
            f"/api/v1/subscriptions/{sub['id']}/items",
            json=[{"product_id": pid, "default_qty": 2, "frequency": "daily"}],
            headers=ah,
        )
        assert r.status_code == 200, r.text
    return shop, sub


async def test_admin_subscriptions_list_filters_and_detail(client):
    ah = auth(await login(client, ADMIN))
    shop, sub = await _shop_with_subscription(client, ah)

    by_shop = await client.get(f"/api/v1/admin/subscriptions?shop_id={shop['id']}", headers=ah)
    assert by_shop.status_code == 200 and by_shop.headers["x-total-count"] == "1"
    row = by_shop.json()[0]
    assert row["id"] == sub["id"] and row["shop_name"] == shop["name"] and row["item_count"] == 1
    assert row["customer_phone"] == ADMIN

    by_account = (await client.get(f"/api/v1/admin/subscriptions?account_id={sub['account_id']}", headers=ah)).json()
    assert sub["id"] in [r["id"] for r in by_account]

    assert (await client.post(f"/api/v1/subscriptions/{sub['id']}/pause", headers=ah)).status_code == 200
    paused = (await client.get("/api/v1/admin/subscriptions?status_filter=paused", headers=ah)).json()
    assert sub["id"] in [r["id"] for r in paused] and all(r["status"] == "paused" for r in paused)
    active = (await client.get("/api/v1/admin/subscriptions?status_filter=active", headers=ah)).json()
    assert sub["id"] not in [r["id"] for r in active]

    detail = (await client.get(f"/api/v1/admin/subscriptions/{sub['id']}", headers=ah)).json()
    assert detail["status"] == "paused" and detail["shop_pincode"] == "302001"
    assert len(detail["items"]) == 1 and detail["items"][0]["default_qty"] == 2


async def test_admin_subscriptions_validation_and_rbac(client):
    ah = auth(await login(client, ADMIN))
    assert (await client.get("/api/v1/admin/subscriptions?status_filter=nope", headers=ah)).status_code == 422
    assert (await client.get("/api/v1/admin/subscriptions/99999999", headers=ah)).status_code == 404
    ch = auth(await login(client, CUSTOMER))
    assert (await client.get("/api/v1/admin/subscriptions", headers=ch)).status_code == 403
    assert (await client.get("/api/v1/admin/subscriptions/1", headers=ch)).status_code == 403


# ---------- (3) users ----------
async def test_admin_users_filters_and_search(client):
    ah = auth(await login(client, ADMIN))
    drivers = await client.get("/api/v1/admin/users?role=delivery", headers=ah)
    assert drivers.status_code == 200
    assert DRIVER in [u["phone"] for u in drivers.json()] and all(u["role"] == "delivery" for u in drivers.json())
    assert int(drivers.headers["x-total-count"]) >= 1
    found = (await client.get(f"/api/v1/admin/users?q={DRIVER[-6:]}", headers=ah)).json()
    assert [u["phone"] for u in found] == [DRIVER]
    # LIKE metacharacters are literal.
    assert (await client.get("/api/v1/admin/users?q=%25", headers=ah)).json() == []
    assert (await client.get("/api/v1/admin/users?role=wizard", headers=ah)).status_code == 422
    assert (await client.get("/api/v1/admin/users?status_filter=gone", headers=ah)).status_code == 422


# ---------- (4) cutoff history / status ----------
async def test_cutoff_runs_history_and_status(client):
    ah = auth(await login(client, ADMIN))
    date = "2043-01-15"
    run = await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=ah)
    assert run.status_code == 200, run.text

    hist = await client.get(f"/api/v1/admin/cutoff/runs?date_from={date}&date_to={date}", headers=ah)
    assert hist.status_code == 200 and int(hist.headers["x-total-count"]) >= 1
    latest = hist.json()[0]
    assert latest["for_delivery_date"] == date and latest["status"] in ("frozen", "partial")
    assert latest["finished_at"] and latest["duration_seconds"] >= 0
    frozen = (await client.get("/api/v1/admin/cutoff/runs?status_filter=frozen", headers=ah)).json()
    assert all(r["status"] == "frozen" for r in frozen)
    assert (await client.get("/api/v1/admin/cutoff/runs?status_filter=meh", headers=ah)).status_code == 422

    st = await client.get("/api/v1/admin/cutoff/status", headers=ah)
    assert st.status_code == 200
    body = st.json()
    assert body["timezone"] == "Asia/Kolkata" and body["next_cutoff_at"] > body["now"]
    assert body["last_run"]["id"] >= latest["id"]
    assert body["standing_changes_frozen"] is False  # the test env disables the cutoff hour
    ch = auth(await login(client, CUSTOMER))
    assert (await client.get("/api/v1/admin/cutoff/status", headers=ch)).status_code == 403


# ---------- (5) shops + delivery assignments ----------
async def test_admin_shop_list_and_detail_with_tier_and_drivers(client):
    ah = auth(await login(client, ADMIN))
    shop, sub = await _shop_with_subscription(client, ah, items=False)
    tid = (await client.post("/api/v1/admin/price-tiers", json={"code": f"G{uuid.uuid4().hex[:6]}", "name": "Gaps"}, headers=ah)).json()["id"]
    assert (await client.post(f"/api/v1/admin/shops/{shop['id']}/tier", json={"price_tier_id": tid}, headers=ah)).status_code == 200
    driver_id = (await client.get(f"/api/v1/admin/users?q={DRIVER}", headers=ah)).json()[0]["id"]
    asg = await client.post("/api/v1/admin/delivery-assignments", json={"delivery_id": driver_id, "shop_id": shop["id"]}, headers=ah)
    assert asg.status_code == 200, asg.text

    rows = await client.get(f"/api/v1/admin/shops?price_tier_id={tid}", headers=ah)
    assert rows.headers["x-total-count"] == "1"
    row = rows.json()[0]
    assert row["id"] == shop["id"] and row["price_tier_id"] == tid and row["subscription_id"] == sub["id"]
    assert [r["id"] for r in (await client.get(f"/api/v1/admin/shops?q={shop['name'][-6:]}", headers=ah)).json()] == [shop["id"]]

    detail = (await client.get(f"/api/v1/admin/shops/{shop['id']}", headers=ah)).json()
    assert detail["drivers"] == [
        {"assignment_id": asg.json()["id"], "delivery_id": driver_id, "full_name": detail["drivers"][0]["full_name"], "phone": DRIVER, "active": True}
    ]
    listed = (await client.get(f"/api/v1/admin/delivery-assignments?shop_id={shop['id']}", headers=ah)).json()
    assert [a["delivery_id"] for a in listed] == [driver_id]

    assert (await client.get("/api/v1/admin/shops/99999999", headers=ah)).status_code == 404
    assert (await client.get("/api/v1/admin/shops?status_filter=closed", headers=ah)).status_code == 422



async def test_delivery_assignments_list_names_and_active_filter(client):
    ah = auth(await login(client, ADMIN))
    shop, _ = await _shop_with_subscription(client, ah, items=False)
    driver = (await client.get(f"/api/v1/admin/users?q={DRIVER}", headers=ah)).json()[0]
    aid = (
        await client.post("/api/v1/admin/delivery-assignments", json={"delivery_id": driver["id"], "shop_id": shop["id"]}, headers=ah)
    ).json()["id"]

    r = await client.get(f"/api/v1/admin/delivery-assignments?shop_id={shop['id']}&active=true", headers=ah)
    assert r.status_code == 200 and r.headers["x-total-count"] == "1"
    (row,) = r.json()
    assert row["id"] == aid and row["active"] is True
    assert row["driver_phone"] == DRIVER and row["driver_status"] == "active"
    assert row["shop_name"] == shop["name"] and row["shop_pincode"] == shop["pincode"] and row["created_at"]

    assert (await client.delete(f"/api/v1/admin/delivery-assignments/{aid}", headers=ah)).status_code == 204
    inactive = await client.get(f"/api/v1/admin/delivery-assignments?shop_id={shop['id']}&active=false", headers=ah)
    assert [a["id"] for a in inactive.json()] == [aid] and inactive.headers["x-total-count"] == "1"
    active = await client.get(f"/api/v1/admin/delivery-assignments?shop_id={shop['id']}&active=true", headers=ah)
    assert active.json() == [] and active.headers["x-total-count"] == "0"

    cust = auth(await login(client, "9876543210"))
    assert (await client.get("/api/v1/admin/delivery-assignments", headers=cust)).status_code == 403


# ---------- (6) unique location names ----------
async def test_location_names_are_unique_ignoring_case_and_spaces(client):
    ah = auth(await login(client, ADMIN))
    name = f"Gaps Hub {uuid.uuid4().hex[:6]}"
    first = await client.post("/api/v1/admin/locations", json={"name": f"  {name} "}, headers=ah)
    assert first.status_code == 200 and first.json()["name"] == name  # trimmed
    dup = await client.post("/api/v1/admin/locations", json={"name": name.upper()}, headers=ah)
    assert dup.status_code == 409 and "already exists" in dup.json()["detail"]
    assert (await client.post("/api/v1/admin/locations", json={"name": "   "}, headers=ah)).status_code == 422

    other = (await client.post("/api/v1/admin/locations", json={"name": f"{name} B"}, headers=ah)).json()
    clash = await client.patch(f"/api/v1/admin/locations/{other['id']}", json={"name": name.lower()}, headers=ah)
    assert clash.status_code == 409
    # Renaming a location to (a recased version of) its own name is fine.
    same = await client.patch(f"/api/v1/admin/locations/{first.json()['id']}", json={"name": name.lower()}, headers=ah)
    assert same.status_code == 200 and same.json()["name"] == name.lower()


@pytest.mark.parametrize("dupe_name", ["Main Warehouse", "main warehouse "])
async def test_migration_renames_existing_duplicates(dupe_name):
    """The 0057 upgrade renames later duplicates to "<name> (#id)" and keeps the oldest."""
    path = pathlib.Path(__file__).parents[1] / "alembic/versions/0057_location_name_unique.py"
    spec = importlib.util.spec_from_file_location("m0057", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def run(sync_conn, fn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            fn()

    original = (await _q("SELECT id, name FROM locations WHERE lower(name) = 'main warehouse'"))[0]
    async with engine.begin() as conn:
        await conn.run_sync(run, mod.downgrade)  # drop the index to stage legacy data
        dupe_id = (
            await conn.execute(
                sa.text("INSERT INTO locations (name, kind, is_default, status) VALUES (:n, 'warehouse', false, 'active') RETURNING id"),
                {"n": dupe_name},
            )
        ).scalar_one()
        await conn.run_sync(run, mod.upgrade)
    try:
        names = dict(await _q("SELECT id, name FROM locations WHERE id IN (:a, :b)", a=original[0], b=dupe_id))
        assert names[original[0]] == original[1]  # the oldest keeps its name
        assert names[dupe_id] == f"{dupe_name.strip()} (#{dupe_id})"
        idx = await _q("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_locations_name_ci'")
        assert idx and "UNIQUE" in idx[0][0]
    finally:
        await _q("DELETE FROM locations WHERE id = :i", i=dupe_id)
