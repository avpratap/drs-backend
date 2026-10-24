"""Customer returns / RMA endpoints (customer self-service + admin resolution)."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, paged, pagination
from app.models.order import Order
from app.models.rma import ReturnRequest, ReturnRequestItem
from app.models.shop import CustomerAccount
from app.models.user import User
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
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(ReturnRequest).order_by(ReturnRequest.id.desc())
    if status_filter:
        q = q.where(ReturnRequest.status == status_filter)
    rows = await paged(db, q, page)
    return await _outs(db, list(rows))


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
