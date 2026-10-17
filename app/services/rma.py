"""Return-request (RMA) lifecycle: create, approve, reject, refund, cancel.

Refund credits the customer's wallet as store credit (append-only ledger via
billing.wallet_adjust). State transitions are guarded with 409.
"""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.rma import ReturnRequest, ReturnRequestItem
from app.services import billing


async def create_request(db: AsyncSession, user, order: Order, items, reason) -> ReturnRequest:
    """Each requested line must be on the order and not exceed the ordered qty."""
    ordered = {
        i.product_id: float(i.qty)
        for i in (
            await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
        ).scalars().all()
    }
    if not items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A return must have at least one item")
    for it in items:
        if it.product_id not in ordered:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Product {it.product_id} is not on order {order.order_no}",
            )
        if float(it.qty) <= 0 or float(it.qty) > ordered[it.product_id]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Return qty for product {it.product_id} must be in (0, {ordered[it.product_id]}]",
            )
    rr = ReturnRequest(
        rma_no="PENDING",
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
    """Value of the returned lines = sum of original order unit_price * returned qty."""
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
    return round(sum(prices.get(li.product_id, 0.0) * float(li.qty) for li in lines), 2)


async def refund(db: AsyncSession, rr: ReturnRequest, amount: float) -> ReturnRequest:
    if rr.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, f"RMA is {rr.status}, must be approved to refund")
    if amount <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Refund amount must be > 0")
    # Never credit more than the value of the returned goods.
    cap = await _returned_value(db, rr)
    if round(float(amount), 2) > cap:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Refund {amount} exceeds returned value {cap}",
        )
    await billing.wallet_adjust(db, rr.account_id, round(float(amount), 2), "refund", "rma", rr.id)
    rr.refund_amount = round(float(amount), 2)
    rr.status = "refunded"
    return rr


async def cancel(db: AsyncSession, rr: ReturnRequest) -> ReturnRequest:
    if rr.status != "requested":
        raise HTTPException(status.HTTP_409_CONFLICT, f"RMA is {rr.status}, cannot cancel")
    rr.status = "cancelled"
    return rr
