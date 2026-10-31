"""Push deep links + rider/customer delivery events for the mobile apps (M4).

(a) FCM messages carry a `data` block (type + ids) so a tap opens the right screen.
(b) route.assigned (cutoff), route.changed (assignment changes) and
    delivery.out_for_delivery (rider starts the day's route) are emitted.
"""
import datetime as dt
import uuid

from conftest import auth, login
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.notification import Notification
from app.services.notifications import senders
from app.services.notifications import service as notify_service

TOKEN = "f" * 152  # classic FCM registration-token length (the 64-char floor is unchanged)


async def _events(user_id: int, event_type: str) -> list[Notification]:
    async with SessionLocal() as db:
        return list((await db.execute(select(Notification).where(
            Notification.user_id == user_id, Notification.event_type == event_type,
        ).order_by(Notification.id))).scalars())


async def _uid(client, h) -> int:
    return (await client.get("/api/v1/auth/me", headers=h)).json()["id"]


# ---------- (a) data payload ----------

def test_push_data_is_an_allow_list_of_string_ids():
    data = senders.push_data({
        "type": "delivery.completed", "delivery_id": 42, "order_id": 7, "date": "2026-11-01",
        "amount": 135.0, "balance": 12.5, "summary": "5 item(s)", "flag": True, "nested": {"x": 1},
    })
    assert data == {"type": "delivery.completed", "delivery_id": "42", "order_id": "7",
                    "date": "2026-11-01"}
    assert senders.push_data(None) == {}


async def test_fcm_message_carries_the_data_block(monkeypatch):
    sent = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"name": "projects/x/messages/1"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            sent.append(json)
            return FakeResp()

    monkeypatch.setattr(senders, "fcm_enabled", lambda: True)
    monkeypatch.setattr(senders, "_fcm_access_token", lambda: "tok")
    monkeypatch.setattr(senders.httpx, "AsyncClient", FakeClient)
    ok, _, _ = await senders.send_push(TOKEN, "Delivered", {"type": "delivery.completed",
                                                             "delivery_id": 9, "amount": 1})
    assert ok
    msg = sent[0]["message"]
    assert msg["notification"] == {"title": "DRS", "body": "Delivered"}
    assert msg["data"] == {"type": "delivery.completed", "delivery_id": "9"}


async def test_dispatcher_passes_event_type_and_ids_to_push(client, monkeypatch):
    h = auth(await login(client, f"7{uuid.uuid4().int % 10**9:09d}"))
    r = await client.post("/api/v1/account/device-tokens", json={"token": TOKEN + uuid.uuid4().hex,
                                                                 "platform": "android"}, headers=h)
    assert r.status_code == 200
    uid = await _uid(client, h)
    seen = []

    async def fake_push(to_addr, body, payload):
        seen.append(payload)
        return True, "m1", None

    monkeypatch.setitem(senders.SENDERS, "push", fake_push)
    async with SessionLocal() as db:
        from app.models.user import User

        user = await db.get(User, uid)
        rows = await notify_service.emit_event(
            db, "delivery.completed", user, {"summary": "1 item(s)", "amount": 27, "delivery_id": 5},
        )
        await db.commit()
        push = next(r for r in rows if r.channel == "push")
        assert await notify_service.deliver_notification(db, push)
    assert seen[0]["type"] == "delivery.completed" and seen[0]["delivery_id"] == 5
    assert senders.push_data(seen[0]) == {"type": "delivery.completed", "delivery_id": "5"}


# ---------- (b) rider + out-for-delivery events ----------

async def _driver_with_shop(client, ah):
    phone = f"8{uuid.uuid4().int % 10**9:09d}"
    rider = (await client.post("/api/v1/admin/users", json={"phone": phone, "role": "delivery"},
                               headers=ah)).json()["id"]
    shop = (await client.post("/api/v1/shops", json={"name": f"Rt {phone}"}, headers=ah)).json()["id"]
    return rider, phone, shop


async def test_route_changed_on_assignment_changes(client):
    ah = auth(await login(client, "9999900001"))
    rider, _phone, shop = await _driver_with_shop(client, ah)
    r = await client.post("/api/v1/admin/delivery-assignments",
                          json={"delivery_id": rider, "shop_id": shop}, headers=ah)
    assert r.status_code == 200
    await client.post("/api/v1/admin/delivery-assignments",  # already active: no new event
                      json={"delivery_id": rider, "shop_id": shop}, headers=ah)
    await client.delete(f"/api/v1/admin/delivery-assignments/{r.json()['id']}", headers=ah)
    shop2 = (await client.post("/api/v1/shops", json={"name": f"Rt2 {rider}"}, headers=ah)).json()["id"]
    await client.put(f"/api/v1/admin/users/{rider}/shop-scope", json={"shop_ids": [shop, shop2]},
                     headers=ah)
    payloads = [(n.payload["added"], n.payload["removed"])
                for n in await _events(rider, "route.changed") if n.channel == "push"]
    assert payloads == [(1, 0), (0, 1), (2, 0)]


async def test_cutoff_tells_each_rider_their_route_is_ready(client):
    ah = auth(await login(client, "9999900001"))
    rider, _phone, shop = await _driver_with_shop(client, ah)
    await client.post("/api/v1/admin/delivery-assignments",
                      json={"delivery_id": rider, "shop_id": shop}, headers=ah)
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=ah)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": 1, "default_qty": 2, "frequency": "daily"}], headers=ah)
    date = "2036-06-06"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=ah)
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=ah)  # same stops
    ready = [n for n in await _events(rider, "route.assigned") if n.channel == "push"]
    assert len(ready) == 1  # re-run with the same route does not re-notify
    assert ready[0].payload == {"date": date, "stops": 1}
    assert "1 stop" in ready[0].body


async def test_rider_starting_route_tells_customers_out_for_delivery(client):
    from app.models.delivery import DeliveryOrder
    from app.services.subscription import today_ist

    ah = auth(await login(client, "9999900001"))
    rider, phone, shop = await _driver_with_shop(client, ah)
    await client.post("/api/v1/admin/delivery-assignments",
                      json={"delivery_id": rider, "shop_id": shop}, headers=ah)
    today = today_ist()
    async with SessionLocal() as db:  # today's open stop on the rider's route
        from app.models.shop import Shop

        acct = (await db.get(Shop, shop)).account_id
        order = DeliveryOrder(delivery_date=today, account_id=acct, shop_id=shop, status="generated")
        db.add(order)
        await db.commit()
        order_id = order.id
    rh = auth(await login(client, phone))
    r = await client.post("/api/v1/delivery/route/start", headers=rh)
    assert r.status_code == 200, r.text
    assert r.json() == {"date": str(today), "stops": 1, "notified": 1}
    again = await client.post("/api/v1/delivery/route/start", headers=rh)
    assert again.json()["notified"] == 0  # idempotent per stop

    owner = await _uid(client, ah)  # the admin owns this shop's account
    events = await _events(owner, "delivery.out_for_delivery")
    assert events and all(n.payload == {"delivery_id": order_id, "date": str(today)} for n in events)

    future = (today + dt.timedelta(days=1)).isoformat()
    assert (await client.post(f"/api/v1/delivery/route/start?date={future}", headers=rh)).status_code == 409
    cust = auth(await login(client, "9876543210"))
    assert (await client.post("/api/v1/delivery/route/start", headers=cust)).status_code == 403


async def test_existing_payloads_now_carry_deep_link_ids(client):
    """order.generated / delivery.completed carry delivery_id for the deep link."""
    ah = auth(await login(client, "9999900001"))
    shop = (await client.post("/api/v1/shops", json={"name": f"DL {uuid.uuid4().hex[:6]}"},
                              headers=ah)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=ah)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": 1, "default_qty": 1, "frequency": "daily"}], headers=ah)
    date = "2036-07-07"
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=ah)
    order = next(o for o in (await client.get(f"/api/v1/delivery/orders?date={date}",
                                              headers=ah)).json() if o["shop_id"] == shop)
    await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json={
        "items": [{"product_id": 1, "qty_delivered": 1}], "returns": []}, headers=ah)
    me = await _uid(client, ah)
    for event in ("order.generated", "delivery.completed"):
        mine = [n for n in await _events(me, event) if n.payload.get("delivery_id") == order["id"]]
        assert mine, event
