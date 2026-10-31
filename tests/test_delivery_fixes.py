"""Delivery fixes: the route endpoint is driver/admin-only (B6), and a delivered order's
line totals and subtotal are NET of door returns, matching total/amount_due and the
invoice (B7)."""
import uuid

from conftest import auth, login

DATE = "2034-04-04"


async def _stop(client, ah, date):
    """Admin-owned (funded) shop with 5 x product 1 daily; returns its order for `date`."""
    shop = (await client.post("/api/v1/shops", json={"name": f"B7 {uuid.uuid4().hex[:8]}"},
                              headers=ah)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=ah)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": 1, "default_qty": 5, "frequency": "daily"},
                           {"product_id": 2, "default_qty": 1, "frequency": "daily"}],
                     headers=ah)
    await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={}, headers=ah)
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=ah)).json()
    return next(o for o in orders if o["shop_id"] == shop)


async def test_delivered_lines_and_subtotal_are_net_of_returns(client):
    ah = auth(await login(client, "9999900001"))
    order = await _stop(client, ah, DATE)
    r = await client.post(f"/api/v1/delivery/orders/{order['id']}/deliver", json={
        "items": [{"product_id": 1, "qty_delivered": 5}, {"product_id": 2, "qty_delivered": 1}],
        "returns": [{"product_id": 1, "qty_returned": 2, "reason": "refused"}],
    }, headers=ah)
    assert r.status_code == 200, r.text
    done = next(o for o in (await client.get(f"/api/v1/delivery/orders?date={DATE}",
                                             headers=ah)).json() if o["id"] == order["id"])
    lines = {i["product_id"]: i for i in done["items"]}
    assert lines[1]["line_total"] == 81.0  # 27 x (5 delivered - 2 returned), not 135
    assert lines[2]["line_total"] == 500.0
    assert done["subtotal"] == done["total"] == done["amount_due"] == 581.0
    assert sum(i["line_total"] for i in done["items"]) == done["total"]

    inv = await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={order['id']}",
                            headers=ah)
    assert inv.status_code == 200, inv.text
    assert inv.json()["total"] == done["total"]  # order figures == the GST invoice


async def test_route_is_delivery_staff_only(client):
    ah = auth(await login(client, "9999900001"))
    assert (await client.get(f"/api/v1/delivery/orders/route?date={DATE}", headers=ah)).status_code == 200
    sub = auth(await login(client, "9999900002"))  # subscriber: shop-scoped orders, not a route
    assert (await client.get("/api/v1/delivery/orders/route", headers=sub)).status_code == 403
    assert (await client.get("/api/v1/delivery/orders", headers=sub)).status_code == 200
    cust = auth(await login(client, "9876543210"))
    assert (await client.get("/api/v1/delivery/orders/route", headers=cust)).status_code == 403
    driver = auth(await login(client, "9999900003"))
    assert (await client.get("/api/v1/delivery/orders/route", headers=driver)).status_code == 200
