"""Return-request (RMA) lifecycle: create, approve, reject, refund, cancel.

Refund credits the customer's wallet as store credit (append-only ledger via
billing.wallet_adjust). State transitions are guarded with 409.
"""
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.order import Order, OrderItem
from app.models.rma import ReturnRequest, ReturnRequestItem
from app.models.shipment import Shipment
from app.services import billing, invoicing
from app.services import refunds as refund_acct


async def create_request(db: AsyncSession, user, order: Order, items, reason) -> ReturnRequest:
    """Each requested line must be on the order and not exceed the (un-returned) ordered qty.
    Only a paid (or partly refunded — the shared refund cap still bounds it), non-cancelled
    order can be returned."""
    if order.payment_status not in ("paid", "partially_refunded"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a paid order can be returned"
        )
    if order.status == "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "Order is cancelled; nothing to return")
    # Goods still on their way can't be "returned" (refunding them as store credit while
    # the courier still delivers them pays out twice) — cancel/refund instead.
    in_transit = await db.scalar(
        select(func.count()).select_from(Shipment).where(
            Shipment.order_id == order.id,
            Shipment.status.in_(("draft", "created", "in_transit", "out_for_delivery")),
        )
    )
    if in_transit:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Order is still in transit; it can be returned after delivery"
        )
    ordered = {
        i.product_id: float(i.qty)
        for i in (
            await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
        ).scalars().all()
    }
    if not items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A return must have at least one item")
    # Subtract quantities already returned on this order by OTHER live RMAs so the total
    # returned per product can never exceed what was ordered (prevents multi-RMA over-refund).
    already_rows = (
        await db.execute(
            select(ReturnRequestItem.product_id, func.sum(ReturnRequestItem.qty))
            .join(ReturnRequest, ReturnRequest.id == ReturnRequestItem.return_request_id)
            .where(
                ReturnRequest.order_id == order.id,
                ReturnRequest.status.notin_(("rejected", "cancelled")),
            )
            .group_by(ReturnRequestItem.product_id)
        )
    ).all()
    already = {pid: float(q or 0) for pid, q in already_rows}
    # Aggregate the requested qty PER product across this request's lines first, so two
    # lines for the same product can't each pass the per-line check (single-request
    # over-return). Validate the per-product TOTAL against what remains.
    requested: dict[int, float] = {}
    for it in items:
        if float(it.qty) <= 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Return qty for product {it.product_id} must be > 0",
            )
        requested[it.product_id] = requested.get(it.product_id, 0.0) + float(it.qty)
    for pid, req_qty in requested.items():
        if pid not in ordered:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Product {pid} is not on order {order.order_no}",
            )
        remaining = round(ordered[pid] - already.get(pid, 0.0), 3)
        if req_qty > remaining:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Return qty for product {pid} ({req_qty}) exceeds remaining {remaining} "
                f"(already returned: {already.get(pid, 0.0)})",
            )
    rr = ReturnRequest(
        rma_no=f"PENDING-{uuid4().hex[:20]}",
        order_id=order.id,
        account_id=order.account_id,
        status="requested",
        reason=reason,
        created_by=user.id,
    )
    db.add(rr)
    await db.flush()
    rr.rma_no = f"RMA{rr.id:06d}"
    for it in items:
        db.add(
            ReturnRequestItem(
                return_request_id=rr.id, product_id=it.product_id, qty=it.qty, reason=it.reason
            )
        )
    return rr


async def approve(db: AsyncSession, rr: ReturnRequest) -> ReturnRequest:
    if rr.status != "requested":
        raise HTTPException(status.HTTP_409_CONFLICT, f"RMA is {rr.status}, cannot approve")
    rr.status = "approved"
    return rr


async def reject(db: AsyncSession, rr: ReturnRequest, note: str | None) -> ReturnRequest:
    if rr.status != "requested":
        raise HTTPException(status.HTTP_409_CONFLICT, f"RMA is {rr.status}, cannot reject")
    rr.status = "rejected"
    rr.resolution = note
    return rr


async def _returned_value(db: AsyncSession, rr: ReturnRequest) -> float:
    """Value actually PAID for the returned lines = sum(unit_price * returned qty), scaled
    by the order's discount factor (total/subtotal) so a refund can never exceed what the
    customer paid when an order-level discount/coupon applied. Prices are GST-inclusive."""
    order = await db.get(Order, rr.order_id)
    prices = {
        i.product_id: float(i.unit_price)
        for i in (
            await db.execute(select(OrderItem).where(OrderItem.order_id == rr.order_id))
        ).scalars().all()
    }
    lines = (
        await db.execute(
            select(ReturnRequestItem).where(ReturnRequestItem.return_request_id == rr.id)
        )
    ).scalars().all()
    gross = sum(prices.get(li.product_id, 0.0) * float(li.qty) for li in lines)
    subtotal = float(order.subtotal) if order and order.subtotal else 0.0
    factor = (float(order.total) / subtotal) if subtotal > 0 else 1.0
    return round(gross * factor, 2)


async def refund(db: AsyncSession, rr: ReturnRequest, amount: float) -> ReturnRequest:
    if rr.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"RMA is {rr.status}, must be approved to refund")
    if amount <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Refund amount must be > 0")
    # Never credit more than the (discount-adjusted) value of the returned goods.
    cap = await _returned_value(db, rr)
    if round(float(amount), 2) > cap:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Refund {amount} exceeds returned value {cap}",
        )
    # One cap across BOTH refund paths (RMA store credit + refund to source), taken under
    # the order lock: a full refund-to-source followed by an RMA refund (or vice versa)
    # must never pay out more than the customer paid.
    order = await refund_acct.lock_order(db, rr.order_id)
    if order is not None:
        headroom = await refund_acct.order_refund_headroom(db, order)
        if round(float(amount), 2) > headroom:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Refund {amount} exceeds what is still refundable on the order ({headroom})",
            )
    await billing.wallet_adjust(db, rr.account_id, round(float(amount), 2), "refund", "rma", rr.id)
    rr.refund_amount = round(float(amount), 2)
    rr.status = "refunded"
    # Issue a GST credit note for the returned lines against the order's tax invoice, if one
    # was generated. One-shot: refund is guarded approved->refunded under a row lock.
    inv = await db.scalar(
        select(Invoice).where(
            Invoice.order_id == rr.order_id, Invoice.is_credit_note.is_(False)
        )
    )
    if inv is not None:
        # SUM per product: two RMA lines for the same product must both be reversed (a
        # dict comprehension kept only the last one, under-reversing GST).
        returned: dict[int, float] = {}
        for li in (
            await db.execute(
                select(ReturnRequestItem).where(ReturnRequestItem.return_request_id == rr.id)
            )
        ).scalars().all():
            returned[li.product_id] = returned.get(li.product_id, 0.0) + float(li.qty)
        # Prorate the credit note to the amount actually refunded (goodwill partials
        # must not over-reverse output GST).
        amt = round(float(amount), 2)
        reason = f"Return {rr.rma_no}"
        before = await invoicing.credited_value(db, inv)
        await invoicing.generate_credit_note_for_lines(db, inv, returned, reason=reason, amount=amt)
        # A value-only credit note from an earlier partial refund to source may already
        # have reversed part of the returned products' value: reverse the shortfall from
        # the other products so the credit note still matches the money handed back.
        short = round(amt - (await invoicing.credited_value(db, inv) - before), 2)
        if short > 0:
            await invoicing.generate_credit_note_for_lines(db, inv, None, reason=reason, amount=short)
    if order is not None and order.payment_status in ("paid", "partially_refunded"):
        # Everything the customer paid is (definitely) handed back -> refunded + full
        # reversal; otherwise the order is now partially refunded (so a later invoice
        # catches the credit up).
        if await refund_acct.refunded_settled(db, order.id) >= round(float(order.total), 2):
            order.payment_status = "refunded"
            if inv is not None:
                await invoicing.build_full_credit_note(db, inv, reason=f"Return {rr.rma_no}")
        else:
            order.payment_status = "partially_refunded"
    return rr


async def cancel(db: AsyncSession, rr: ReturnRequest) -> ReturnRequest:
    if rr.status != "requested":
        raise HTTPException(status.HTTP_409_CONFLICT, f"RMA is {rr.status}, cannot cancel")
    rr.status = "cancelled"
    return rr
