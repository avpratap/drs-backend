"""Mobile M5 customer-account flows, end to end through the API with the operations the
app's generated client calls: profile, saved addresses, notification preferences
(per-channel toggles + marketing consents), product ratings/reviews and logout
de-registration of the push token via `delete_device_token`."""
import uuid

from conftest import auth, login
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.device_token import DeviceToken


def _phone() -> str:
    return f"7{uuid.uuid4().int % 10**9:09d}"


ADDR = {"line1": "12 MG Road", "city": "Pune", "state": "Maharashtra", "pincode": "411001"}


async def test_profile_view_update_and_notify_toggles(client):
    h = auth(await login(client, _phone()))
    r = await client.get("/api/v1/profile", headers=h)
    assert r.status_code == 200
    body = r.json()
    for key in ("user_id", "phone", "notify_whatsapp", "notify_sms", "notify_push",
                "notify_email", "marketing_opt_in", "quiet_hours_start", "quiet_hours_end"):
        assert key in body, key

    r = await client.patch("/api/v1/profile", json={
        "full_name": "Asha Rao", "display_name": "Asha", "notify_push": False, "notify_sms": False,
    }, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["full_name"] == "Asha Rao" and out["display_name"] == "Asha"
    assert out["notify_push"] is False and out["notify_sms"] is False
    assert (await client.get("/api/v1/profile", headers=h)).json()["notify_push"] is False


async def test_saved_address_crud_and_default(client):
    h = auth(await login(client, _phone()))
    r1 = await client.post("/api/v1/addresses", json={**ADDR, "label": "Home"}, headers=h)
    assert r1.status_code == 201, r1.text
    r2 = await client.post("/api/v1/addresses", json={**ADDR, "label": "Work", "line1": "Office"},
                           headers=h)
    assert r2.status_code == 201
    a1, a2 = r1.json()["id"], r2.json()["id"]

    listed = (await client.get("/api/v1/addresses", headers=h)).json()
    assert {a["id"] for a in listed} == {a1, a2}

    r = await client.patch(f"/api/v1/addresses/{a2}", json={"landmark": "Near park"}, headers=h)
    assert r.status_code == 200 and r.json()["landmark"] == "Near park"

    r = await client.post(f"/api/v1/addresses/{a2}/default", headers=h)
    assert r.status_code == 200 and r.json()["is_default"] is True
    listed = (await client.get("/api/v1/addresses", headers=h)).json()
    assert listed[0]["id"] == a2  # default first
    assert [a["id"] for a in listed if a["is_default"]] == [a2]  # exactly one default

    # Someone else can't see, edit, default or delete it.
    other = auth(await login(client, _phone()))
    assert (await client.get("/api/v1/addresses", headers=other)).json() == []
    assert (await client.patch(f"/api/v1/addresses/{a1}", json={"label": "x"},
                               headers=other)).status_code == 404
    assert (await client.post(f"/api/v1/addresses/{a1}/default", headers=other)).status_code == 404
    assert (await client.delete(f"/api/v1/addresses/{a1}", headers=other)).status_code == 404

    assert (await client.delete(f"/api/v1/addresses/{a1}", headers=h)).status_code == 204
    assert [a["id"] for a in (await client.get("/api/v1/addresses", headers=h)).json()] == [a2]
    assert (await client.delete(f"/api/v1/addresses/{a1}", headers=h)).status_code == 404


async def test_marketing_consents_drive_notification_preferences(client):
    h = auth(await login(client, _phone()))
    r = await client.post("/api/v1/consents", json={"purpose": "marketing_push",
                                                    "action": "granted"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["purpose"] == "marketing_push" and r.json()["action"] == "granted"
    assert (await client.get("/api/v1/consents", headers=h)).json()["marketing_push"] == "granted"
    assert (await client.get("/api/v1/profile", headers=h)).json()["marketing_opt_in"] is True

    await client.post("/api/v1/consents", json={"purpose": "marketing_push",
                                                "action": "withdrawn"}, headers=h)
    assert (await client.get("/api/v1/consents", headers=h)).json()["marketing_push"] == "withdrawn"
    assert (await client.get("/api/v1/profile", headers=h)).json()["marketing_opt_in"] is False
    bad = await client.post("/api/v1/consents", json={"purpose": "nope", "action": "granted"},
                            headers=h)
    assert bad.status_code == 422


async def test_ratings_submit_list_helpful_delete(client):
    from app.models.rating import Rating

    pid = 1
    h = auth(await login(client, _phone()))
    r = await client.post(f"/api/v1/products/{pid}/ratings",
                          json={"stars": 4, "comment": "Fresh and creamy"}, headers=h)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    mine = (await client.get(f"/api/v1/products/{pid}/ratings", headers=h)).json()
    assert {"product_id", "count", "average", "distribution", "ratings"} <= set(mine)
    row = next(x for x in mine["ratings"] if x["id"] == rid)
    assert row["mine"] is True and row["stars"] == 4

    async with SessionLocal() as db:  # publish it (no purchase -> held for moderation)
        (await db.get(Rating, rid)).status = "approved"
        await db.commit()

    voter = auth(await login(client, _phone()))
    v = await client.post(f"/api/v1/products/{pid}/ratings/{rid}/helpful", headers=voter)
    assert v.status_code == 200 and v.json() == {"rating_id": rid, "helpful_count": 1, "voted": True}
    assert (await client.post(f"/api/v1/products/{pid}/ratings/{rid}/helpful",
                              headers=h)).status_code == 409  # not on your own review
    public = (await client.get(f"/api/v1/products/{pid}/ratings")).json()
    seen = next(x for x in public["ratings"] if x["id"] == rid)
    assert seen["user_id"] is None and seen["mine"] is False  # reviewer stays anonymous

    assert (await client.delete(f"/api/v1/products/{pid}/ratings", headers=h)).status_code == 204
    assert (await client.delete(f"/api/v1/products/{pid}/ratings", headers=h)).status_code == 404


async def _active(token: str) -> bool | None:
    async with SessionLocal() as db:
        return await db.scalar(select(DeviceToken.active).where(DeviceToken.token == token))


async def test_delete_device_token_deregisters_on_logout(client):
    token = "d" * 152 + uuid.uuid4().hex
    h = auth(await login(client, _phone()))
    r = await client.post("/api/v1/account/device-tokens",
                          json={"token": token, "platform": "android"}, headers=h)
    assert r.status_code == 200 and r.json()["active"] is True

    # Another user can't deactivate it, and gets the same 204 (no probing).
    other = auth(await login(client, _phone()))
    r = await client.request("DELETE", "/api/v1/account/device-tokens", json={"token": token},
                             headers=other)
    assert r.status_code == 204 and await _active(token) is True

    r = await client.request("DELETE", "/api/v1/account/device-tokens", json={"token": token},
                             headers=h)
    assert r.status_code == 204 and await _active(token) is False
    # Idempotent, and the older unregister forms still work.
    assert (await client.request("DELETE", "/api/v1/account/device-tokens", json={"token": token},
                                 headers=h)).status_code == 204
    assert (await client.post("/api/v1/account/device-tokens/unregister", json={"token": token},
                              headers=h)).status_code == 204
    assert (await client.request("DELETE", "/api/v1/account/device-tokens",
                                 headers=h)).status_code == 422  # body required
