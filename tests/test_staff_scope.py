"""Admin staff management: list staff with their shop scope, and get/set a delivery
staff member's multi-shop scope (enforced via app.deps.scope.allowed_shop_ids)."""
import uuid

from conftest import auth, login
from sqlalchemy import func, select

from app.core.db import SessionLocal


def _phone() -> str:
    return f"8{uuid.uuid4().int % 10**9:09d}"


async def _driver(client, ah) -> tuple[int, str]:
    phone = _phone()
    r = await client.post("/api/v1/admin/users",
                          json={"phone": phone, "role": "delivery", "full_name": "Driver"}, headers=ah)
    assert r.status_code == 201, r.text
    return r.json()["id"], phone


async def _shop(client, ah) -> int:
    r = await client.post("/api/v1/shops", json={"name": f"Scope {uuid.uuid4().hex[:8]}"}, headers=ah)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _audits(user_id: int) -> int:
    from app.models.admin_tools import AdminAuditLog

    async with SessionLocal() as db:
        return await db.scalar(
            select(func.count()).select_from(AdminAuditLog).where(
                AdminAuditLog.action == "staff_shop_scope", AdminAuditLog.resource_id == str(user_id)
            )
        )


async def test_set_delivery_scope_is_enforced(client):
    ah = auth(await login(client, "9999900001"))
    uid, phone = await _driver(client, ah)
    s1, s2, s3 = [await _shop(client, ah) for _ in range(3)]
    url = f"/api/v1/admin/users/{uid}/shop-scope"

    r = await client.put(url, json={"shop_ids": [s2, s1, s1]}, headers=ah)  # dupes collapse
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": uid, "role": "delivery", "all_shops": False,
                        "shop_ids": sorted([s1, s2]), "editable": True,
                        "source": "delivery_assignments"}
    driver = auth(await login(client, phone))
    seen = {s["id"] for s in (await client.get("/api/v1/shops", headers=driver)).json()}
    assert seen == {s1, s2}  # authorization follows the scope

    r = await client.put(url, json={"shop_ids": [s2, s3]}, headers=ah)
    assert r.json()["shop_ids"] == sorted([s2, s3])
    seen = {s["id"] for s in (await client.get("/api/v1/shops", headers=driver)).json()}
    assert seen == {s2, s3}
    # s1 was deactivated, not deleted (history kept).
    rows = (await client.get(f"/api/v1/admin/delivery-assignments?delivery_id={uid}", headers=ah)).json()
    assert {(x["shop_id"], x["active"]) for x in rows} == {(s1, False), (s2, True), (s3, True)}

    assert (await client.get(url, headers=ah)).json()["shop_ids"] == sorted([s2, s3])
    assert await _audits(uid) == 2
    await client.put(url, json={"shop_ids": [s3, s2]}, headers=ah)  # no change -> no audit
    assert await _audits(uid) == 2

    r = await client.put(url, json={"shop_ids": []}, headers=ah)  # clear
    assert r.json()["shop_ids"] == []
    assert (await client.get("/api/v1/shops", headers=driver)).json() == []


async def test_scope_errors(client):
    ah = auth(await login(client, "9999900001"))
    uid, _ = await _driver(client, ah)
    r = await client.put(f"/api/v1/admin/users/{uid}/shop-scope",
                         json={"shop_ids": [999999999]}, headers=ah)
    assert r.status_code == 422 and "999999999" in r.text
    assert (await client.get("/api/v1/admin/users/999999999/shop-scope", headers=ah)).status_code == 404
    assert (await client.put("/api/v1/admin/users/999999999/shop-scope",
                             json={"shop_ids": []}, headers=ah)).status_code == 404
    # Admin / subscriber scope isn't editable here.
    users = (await client.get("/api/v1/admin/users?role=subscriber&q=9999900002", headers=ah)).json()
    sub_id = users[0]["id"]
    assert (await client.put(f"/api/v1/admin/users/{sub_id}/shop-scope",
                             json={"shop_ids": []}, headers=ah)).status_code == 409
    me = (await client.get("/api/v1/auth/me", headers=ah)).json()["id"]
    assert (await client.put(f"/api/v1/admin/users/{me}/shop-scope",
                             json={"shop_ids": []}, headers=ah)).status_code == 409
    # Admin-only.
    cust = auth(await login(client, "9876543210"))
    assert (await client.get(f"/api/v1/admin/users/{uid}/shop-scope", headers=cust)).status_code == 403
    assert (await client.put(f"/api/v1/admin/users/{uid}/shop-scope",
                             json={"shop_ids": []}, headers=cust)).status_code == 403
    assert (await client.get("/api/v1/admin/staff", headers=cust)).status_code == 403


async def test_get_scope_reports_admin_and_ownership(client):
    ah = auth(await login(client, "9999900001"))
    me = (await client.get("/api/v1/auth/me", headers=ah)).json()["id"]
    r = (await client.get(f"/api/v1/admin/users/{me}/shop-scope", headers=ah)).json()
    assert r["all_shops"] is True and r["shop_ids"] == [] and r["source"] == "admin"
    assert r["editable"] is False

    sub = (await client.get("/api/v1/admin/users?role=subscriber&q=9999900002", headers=ah)).json()[0]
    r = (await client.get(f"/api/v1/admin/users/{sub['id']}/shop-scope", headers=ah)).json()
    assert r["source"] == "account_ownership" and r["editable"] is False and r["shop_ids"]
    own = {s["id"] for s in (await client.get(
        "/api/v1/shops", headers=auth(await login(client, "9999900002")))).json()}
    assert set(r["shop_ids"]) == own


async def test_list_staff_with_scope(client):
    ah = auth(await login(client, "9999900001"))
    uid, phone = await _driver(client, ah)
    s1 = await _shop(client, ah)
    await client.put(f"/api/v1/admin/users/{uid}/shop-scope", json={"shop_ids": [s1]}, headers=ah)

    r = await client.get("/api/v1/admin/staff?limit=200", headers=ah)
    assert r.status_code == 200 and int(r.headers["x-total-count"]) >= 2
    rows = r.json()
    assert {x["role"] for x in rows} <= {"admin", "delivery"}  # no customers
    assert all(x["all_shops"] for x in rows if x["role"] == "admin")

    found = (await client.get(f"/api/v1/admin/staff?role=delivery&q={phone}", headers=ah)).json()
    assert [(x["id"], x["shop_ids"], x["all_shops"]) for x in found] == [(uid, [s1], False)]
    # Suspended staff are still listed (filterable); erased ones are not.
    await client.patch(f"/api/v1/admin/users/{uid}", json={"status": "suspended"}, headers=ah)
    sus = (await client.get(f"/api/v1/admin/staff?status_filter=suspended&q={phone}", headers=ah)).json()
    assert [x["status"] for x in sus] == ["suspended"]
