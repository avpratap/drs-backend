"""Customer returns / RMA endpoints (customer self-service + admin resolution)."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, paged, pagination
from app.models.billing import Wallet, WalletTransaction
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.rma import RMA_STATUSES, ReturnRequest, ReturnRequestItem
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import refunds as refund_acct
from app.services import rma as rma_service

router = APIRouter(tags=["returns"])


# ---------- Schemas ----------
class RMAItemIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=100_000, allow_inf_nan=False)
    reason: str | None = Field(default=None, max_length=255)


class RMACreateIn(BaseModel):
    order_id: int
    items: list[RMAItemIn] = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=255)


class RejectIn(BaseModel):
    note: str | None = Field(default=None, max_length=255)


class RefundIn(BaseModel):
    amount: float = Field(gt=0, le=10_000_000, allow_inf_nan=False)


class RMAItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty: float
    reason: str | None = None


class RMAOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    rma_no: str
    order_id: int
    account_id: int
    status: str
    reason: str | None = None
    resolution: str | None = None
    refund_amount: float
    items: list[RMAItemOut] = []
    created_at: dt.datetime | None = None


class RMARefundCreditOut(BaseModel):
    """Store credit paid for a return (a wallet ledger row with ref_type='rma')."""

    id: int
    amount: float
    balance_after: float
    created_at: dt.datetime


class RMACreditNoteOut(BaseModel):
    id: int
    invoice_no: str
    invoice_date: dt.date
    total: float


class AdminRMADetailOut(RMAOut):
    order_no: str | None = None
    order_total: float | None = None
    order_payment_status: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    # Value of the returned lines (discount-adjusted): the most a refund may pay.
    returned_value: float = 0
    # What the order can still refund across gateway refunds + RMA credit.
    refundable_headroom: float = 0
    refund_credits: list[RMARefundCreditOut] = []
    credit_notes: list[RMACreditNoteOut] = []


async def _my_account_ids(db: AsyncSession, user: User) -> set[int]:
    return set(
        (
            await db.execute(
                select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id)
            )
        ).scalars().all()
    )


async def _out(db: AsyncSession, rr: ReturnRequest) -> RMAOut:
    items = (
        await db.execute(
            select(ReturnRequestItem).where(ReturnRequestItem.return_request_id == rr.id)
        )
    ).scalars().all()
    out = RMAOut.model_validate(rr)
    out.items = [RMAItemOut.model_validate(i) for i in items]
    return out


async def _outs(db: AsyncSession, rows: list[ReturnRequest]) -> list[RMAOut]:
    """List views with ONE batched item query (was one query per return — N+1)."""
    if not rows:
        return []
    by_rr: dict[int, list[RMAItemOut]] = {}
    for it in (
        await db.execute(
            select(ReturnRequestItem).where(
                ReturnRequestItem.return_request_id.in_([r.id for r in rows])
            )
        )
    ).scalars():
        by_rr.setdefault(it.return_request_id, []).append(RMAItemOut.model_validate(it))
    outs = []
    for rr in rows:
        o = RMAOut.model_validate(rr)
        o.items = by_rr.get(rr.id, [])
        outs.append(o)
    return outs


async def _owned_or_404(db: AsyncSession, rr_id: int, user: User, *, lock: bool = False) -> ReturnRequest:
    q = select(ReturnRequest).where(ReturnRequest.id == rr_id)
    if lock:
        q = q.with_for_update()
    rr = (await db.execute(q)).scalar_one_or_none()
    if rr is None or (user.role != "admin" and rr.account_id not in await _my_account_ids(db, user)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Return not found")
    return rr


# ---------- Customer ----------
@router.post("/returns", response_model=RMAOut, operation_id="create_return")
async def create_return(
    data: RMACreateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    # Lock the order so two concurrent returns can't each pass the over-return guard
    # against the same baseline (TOCTOU aggregate over-return).
    order = (
        await db.execute(select(Order).where(Order.id == data.order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None or (
        user.role != "admin" and order.account_id not in await _my_account_ids(db, user)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    rr = await rma_service.create_request(db, user, order, data.items, data.reason)
    await db.commit()
    await db.refresh(rr)
    return await _out(db, rr)


@router.get("/returns", response_model=list[RMAOut], operation_id="list_my_returns")
async def list_my_returns(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    ids = await _my_account_ids(db, user)
    if not ids:
        return []
    rows = (
        await db.execute(
            select(ReturnRequest)
            .where(ReturnRequest.account_id.in_(ids))
            .order_by(ReturnRequest.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
    ).scalars().all()
    return await _outs(db, list(rows))


@router.get("/returns/{rr_id}", response_model=RMAOut, operation_id="get_return")
async def get_return(rr_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rr = await _owned_or_404(db, rr_id, user)
    return await _out(db, rr)


@router.post("/returns/{rr_id}/cancel", response_model=RMAOut, operation_id="cancel_return")
async def cancel_return(rr_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rr = await _owned_or_404(db, rr_id, user, lock=True)
    await rma_service.cancel(db, rr)
    await db.commit()
    await db.refresh(rr)
    return await _out(db, rr)


# ---------- Admin ----------
@router.get("/admin/returns", response_model=list[RMAOut], operation_id="admin_list_returns",
            dependencies=[Depends(require_role("admin"))])
async def admin_list_returns(
    status_filter: str | None = None,
    account_id: int | None = None,
    order_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    if status_filter is not None and status_filter not in RMA_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"status_filter must be one of {', '.join(RMA_STATUSES)}"
        )
    q = select(ReturnRequest).order_by(ReturnRequest.id.desc())
    if status_filter:
        q = q.where(ReturnRequest.status == status_filter)
    if account_id is not None:
        q = q.where(ReturnRequest.account_id == account_id)
    if order_id is not None:
        q = q.where(ReturnRequest.order_id == order_id)
    rows = await paged(db, q, page)
    return await _outs(db, list(rows))


@router.get("/admin/returns/{rr_id}", response_model=AdminRMADetailOut, operation_id="admin_get_return",
            dependencies=[Depends(require_role("admin"))])
async def admin_get_return(rr_id: int, db: AsyncSession = Depends(get_db)):
    """One return with its order, customer, refund limits and the refund it produced:
    the store-credit wallet entry and any GST credit notes on the order."""
    rr = await db.get(ReturnRequest, rr_id)
    if rr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Return not found")
    out = AdminRMADetailOut.model_validate((await _out(db, rr)).model_dump())
    order = await db.get(Order, rr.order_id)
    if order is not None:
        out.order_no = order.order_no
        out.order_total = float(order.total)
        out.order_payment_status = order.payment_status
        out.refundable_headroom = await refund_acct.order_refund_headroom(db, order)
        out.credit_notes = [
            RMACreditNoteOut(id=i.id, invoice_no=i.invoice_no, invoice_date=i.invoice_date, total=float(i.total))
            for i in (
                await db.execute(
                    select(Invoice)
                    .where(Invoice.order_id == order.id, Invoice.is_credit_note.is_(True))
                    .order_by(Invoice.id)
                )
            ).scalars()
        ]
    acct = await db.get(CustomerAccount, rr.account_id)
    owner = await db.get(User, acct.owner_user_id) if acct else None
    if owner is not None:
        out.customer_name = owner.full_name
        out.customer_phone = owner.phone
    out.returned_value = await rma_service.returned_value(db, rr)
    out.refund_credits = [
        RMARefundCreditOut(
            id=t.id, amount=float(t.amount), balance_after=float(t.balance_after), created_at=t.created_at
        )
        for t in (
            await db.execute(
                select(WalletTransaction)
                .join(Wallet, Wallet.id == WalletTransaction.wallet_id)
                .where(
                    Wallet.account_id == rr.account_id,
                    WalletTransaction.ref_type == "rma",
                    WalletTransaction.ref_id == rr.id,
                )
                .order_by(WalletTransaction.id)
            )
        ).scalars()
    ]
    return out


async def _locked_admin(db: AsyncSession, rr_id: int) -> ReturnRequest:
    rr = (
        await db.execute(select(ReturnRequest).where(ReturnRequest.id == rr_id).with_for_update())
    ).scalar_one_or_none()
    if rr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Return not found")
    return rr


@router.post("/admin/returns/{rr_id}/approve", response_model=RMAOut, operation_id="approve_return",
             dependencies=[Depends(require_role("admin"))])
async def approve_return(rr_id: int, db: AsyncSession = Depends(get_db)):
    rr = await _locked_admin(db, rr_id)
    await rma_service.approve(db, rr)
    await db.commit()
    await db.refresh(rr)
    return await _out(db, rr)


@router.post("/admin/returns/{rr_id}/reject", response_model=RMAOut, operation_id="reject_return",
             dependencies=[Depends(require_role("admin"))])
async def reject_return(rr_id: int, data: RejectIn, db: AsyncSession = Depends(get_db)):
    rr = await _locked_admin(db, rr_id)
    await rma_service.reject(db, rr, data.note)
    await db.commit()
    await db.refresh(rr)
    return await _out(db, rr)


@router.post("/admin/returns/{rr_id}/refund", response_model=RMAOut, operation_id="refund_return",
             dependencies=[Depends(require_role("admin"))])
async def refund_return(rr_id: int, data: RefundIn, db: AsyncSession = Depends(get_db)):
    rr = await _locked_admin(db, rr_id)
    await rma_service.refund(db, rr, data.amount)
    await db.commit()
    await db.refresh(rr)
    return await _out(db, rr)
