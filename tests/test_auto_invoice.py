"""GST tax invoice auto-issued when a delivery is marked delivered.

- Same DB transaction as the delivery (a delivery that rolls back leaves no invoice).
- Exactly one per delivery order: retries / re-delivery / the manual endpoint never
  duplicate (advisory lock + uq_invoice_delivery_order).
- Bills the NET delivered qty per line (partial delivery: what was supplied; failed or
  fully returned: no invoice), with HSN, GST rate and CGST/SGST (or IGST) per line.
- Same per-FY serial as the manual path; issued tax invoices are append-only (trigger).
- POST /admin/invoices/generate still works as a backfill for older deliveries.
"""
import uuid

import pytest
from conftest import auth, login
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.invoice import Invoice, InvoiceLine

MILK, GHEE = 1, 2  # test seed: milk Rs 27 (0% GST), ghee Rs 500 (12% GST, HSN 04059020)


async def _stop(client, ah, date, *, items=((MILK, 2), (GHEE, 1)), state=None):
    """An admin-account shop subscribed to `items`, cut off for `date`: its open order."""
    body = {"name": f"Inv {uuid.uuid4().hex[:8]}"}
    if state:
        body["state"] = state
    shop = (await client.post("/api/v1/shops", json=body, headers=ah)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=ah)).json()["id"]
    r = await client.put(f"/api/v1/subscriptions/{sid}/items", json=[
        {"product_id": p, "default_qty": q, "frequency": "daily"} for p, q in items], headers=ah)
    assert r.status_code == 200, r.text
    assert (await client.post(f"/api/v1/admin/cutoff/run?date={date}", json={},
                              headers=ah)).status_code == 200
    orders = (await client.get(f"/api/v1/delivery/orders?date={date}", headers=ah)).json()
    return next(o for o in orders if o["shop_id"] == shop)


async def _deliver(client, ah, order_id, qtys, returns=()):
    return await client.post(f"/api/v1/delivery/orders/{order_id}/deliver", json={
        "items": [{"product_id": p, "qty_delivered": q} for p, q in qtys.items()],
        "returns": [{"product_id": p, "qty_returned": q, "reason": "refused"} for p, q in returns],
    }, headers=ah)


async def _invoices(order_id) -> list[Invoice]:
    async with SessionLocal() as db:
        return list((await db.execute(select(Invoice).where(
            Invoice.delivery_order_id == order_id, Invoice.is_credit_note.is_(False)))).scalars())


async def _lines(invoice_id) -> list[InvoiceLine]:
    async with SessionLocal() as db:
        return list((await db.execute(select(InvoiceLine).where(
            InvoiceLine.invoice_id == invoice_id).order_by(InvoiceLine.product_id))).scalars())


async def test_delivery_issues_exactly_one_correct_tax_invoice(client):
    ah = auth(await login(client, "9999900001"))
    order = await _stop(client, ah, "2039-02-01")
    r = await _deliver(client, ah, order["id"], {MILK: 2, GHEE: 1})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "delivered"

    [inv] = await _invoices(order["id"])  # issued by the delivery itself, no extra call
    assert inv.invoice_no == f"{settings.INVOICE_PREFIX}/{inv.fy}/{inv.seq_no:06d}"
    assert not inv.is_interstate and float(inv.igst) == 0.0
    milk, ghee = await _lines(inv.id)
    # Milk: 2 x Rs 27 at 0% GST.
    assert (milk.product_id, float(milk.qty), float(milk.line_total)) == (MILK, 2.0, 54.0)
    assert float(milk.gst_rate) == 0 and float(milk.taxable_value) == 54.0
    assert float(milk.cgst) == float(milk.sgst) == 0.0
    # Ghee: Rs 500 GST-inclusive at 12% -> taxable 446.42 + CGST 26.79 + SGST 26.79.
    assert (ghee.product_id, ghee.hsn_code, float(ghee.gst_rate)) == (GHEE, "04059020", 12.0)
    assert (float(ghee.taxable_value), float(ghee.cgst), float(ghee.sgst)) == (446.42, 26.79, 26.79)
    assert float(ghee.line_total) == 500.0
    # Document totals are the sum of the lines, and equal what the delivery billed.
    assert float(inv.total) == 554.0 == r.json()["total"]
    assert float(inv.taxable_value) == 500.42
    assert float(inv.cgst) == float(inv.sgst) == 26.79
    assert round(float(inv.taxable_value) + float(inv.cgst) + float(inv.sgst), 2) == 554.0

    # A retried / repeated delivery is refused and never issues a second invoice...
    again = await _deliver(client, ah, order["id"], {MILK: 2, GHEE: 1})
    assert again.status_code == 409
    # ...and the manual endpoint returns the same document.
    m = await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={order['id']}",
                          headers=ah)
    assert m.status_code == 200 and m.json()["invoice_no"] == inv.invoice_no
    assert len(await _invoices(order["id"])) == 1


async def test_auto_and_manual_share_one_gap_free_serial(client, monkeypatch):
    """Backfill: a delivery made before auto-invoicing shipped has no invoice; the manual
    endpoint issues it, numbered in the same per-FY series as auto-issued ones."""
    from app.services import delivery as delivery_svc

    ah = auth(await login(client, "9999900001"))
    legacy = await _stop(client, ah, "2039-02-02", items=((MILK, 1),))
    auto = await _stop(client, ah, "2039-02-03", items=((MILK, 1),))

    async def not_yet_shipped(db, order):  # the pre-auto-invoice delivery path
        return None

    monkeypatch.setattr(delivery_svc.invoicing, "issue_delivery_invoice", not_yet_shipped)
    assert (await _deliver(client, ah, legacy["id"], {MILK: 1})).status_code == 200
    monkeypatch.undo()
    assert await _invoices(legacy["id"]) == []

    assert (await _deliver(client, ah, auto["id"], {MILK: 1})).status_code == 200
    [a] = await _invoices(auto["id"])
    r = await client.post(f"/api/v1/admin/invoices/generate?delivery_order_id={legacy['id']}",
                          headers=ah)
    assert r.status_code == 200, r.text
    [m] = await _invoices(legacy["id"])
    assert m.fy == a.fy and m.seq_no == a.seq_no + 1  # next number, no gap, no clash
    assert r.json()["invoice_no"] == m.invoice_no and r.json()["total"] == 27.0
    again = await client.post(
        f"/api/v1/admin/invoices/generate?delivery_order_id={legacy['id']}", headers=ah)
    assert again.json()["invoice_no"] == m.invoice_no  # idempotent backfill


async def test_partial_delivery_invoices_only_what_was_supplied(client):
    ah = auth(await login(client, "9999900001"))
    order = await _stop(client, ah, "2039-02-04", items=((MILK, 4), (GHEE, 2)))
    # 3 of 4 milk delivered (1 of them refused at the door), no ghee at all.
    r = await _deliver(client, ah, order["id"], {MILK: 3, GHEE: 0}, returns=[(MILK, 1)])
    assert r.status_code == 200 and r.json()["status"] == "partially_delivered"
    [inv] = await _invoices(order["id"])
    [line] = await _lines(inv.id)  # ghee: nothing supplied -> no line
    assert (line.product_id, float(line.qty), float(line.line_total)) == (MILK, 2.0, 54.0)
    assert float(inv.total) == 54.0 == r.json()["total"]  # == what was billed


async def test_failed_or_fully_returned_stop_gets_no_invoice(client):
    ah = auth(await login(client, "9999900001"))
    failed = await _stop(client, ah, "2039-02-05", items=((MILK, 2),))
    r = await _deliver(client, ah, failed["id"], {MILK: 0})
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert await _invoices(failed["id"]) == []

    returned = await _stop(client, ah, "2039-02-06", items=((MILK, 2),))
    r = await _deliver(client, ah, returned["id"], {MILK: 2}, returns=[(MILK, 2)])
    assert r.status_code == 200
    assert await _invoices(returned["id"]) == []  # nothing supplied: no serial burned
    m = await client.post(
        f"/api/v1/admin/invoices/generate?delivery_order_id={returned['id']}", headers=ah)
    assert m.status_code == 409


async def test_interstate_delivery_uses_igst(client):
    ah = auth(await login(client, "9999900001"))
    order = await _stop(client, ah, "2039-02-07", items=((GHEE, 1),), state="Maharashtra")
    assert (await _deliver(client, ah, order["id"], {GHEE: 1})).status_code == 200
    [inv] = await _invoices(order["id"])
    assert inv.is_interstate
    assert (float(inv.taxable_value), float(inv.igst)) == (446.43, 53.57)
    assert float(inv.cgst) == float(inv.sgst) == 0.0 and float(inv.total) == 500.0


async def test_delivery_rollback_leaves_no_invoice(client, monkeypatch):
    """The invoice lives in the delivery's transaction: if the delivery fails after the
    invoice was built, neither is persisted and the stop stays open."""
    from app.services import delivery as delivery_svc

    ah = auth(await login(client, "9999900001"))
    order = await _stop(client, ah, "2039-02-08", items=((MILK, 1),))

    async def boom(*a, **k):
        raise RuntimeError("crash after the invoice was issued")

    monkeypatch.setattr(delivery_svc.notify, "emit_event", boom)
    with pytest.raises(RuntimeError):
        await _deliver(client, ah, order["id"], {MILK: 1})
    monkeypatch.undo()
    assert await _invoices(order["id"]) == []
    orders = (await client.get("/api/v1/delivery/orders?date=2039-02-08", headers=ah)).json()
    assert next(o for o in orders if o["id"] == order["id"])["status"] == "generated"
    # Delivering it now works, and issues the invoice (the serial was never burned).
    assert (await _deliver(client, ah, order["id"], {MILK: 1})).status_code == 200
    assert len(await _invoices(order["id"])) == 1


async def test_issued_tax_invoice_is_append_only(client):
    ah = auth(await login(client, "9999900001"))
    order = await _stop(client, ah, "2039-02-09", items=((MILK, 1),))
    assert (await _deliver(client, ah, order["id"], {MILK: 1})).status_code == 200
    [inv] = await _invoices(order["id"])

    for stmt in (
        "UPDATE invoices SET total = 1 WHERE id = :i",
        "DELETE FROM invoices WHERE id = :i",
        "UPDATE invoice_lines SET line_total = 1 WHERE invoice_id = :i",
        "DELETE FROM invoice_lines WHERE invoice_id = :i",
    ):
        async with SessionLocal() as db:
            with pytest.raises(DBAPIError, match="append-only"):
                await db.execute(text(stmt), {"i": inv.id})
            await db.rollback()
    async with SessionLocal() as db:  # caching the rendered PDF's key is allowed
        await db.execute(text("UPDATE invoices SET pdf_url = 'k' WHERE id = :i"), {"i": inv.id})
        await db.commit()
    [after] = await _invoices(order["id"])
    assert float(after.total) == 27.0 and after.pdf_url == "k"
