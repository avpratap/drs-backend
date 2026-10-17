from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, pagination
from app.models.transfer import StockTransfer, StockTransferItem
from app.models.user import User
from app.services import transfers

router = APIRouter(
    prefix="/admin", tags=["transfers"], dependencies=[Depends(require_role("admin"))]
)


class TransferItemIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)


class TransferIn(BaseModel):
    from_location_id: int | None = None
    to_location_id: int
    note: str | None = Field(default=None, max_length=255)
    items: list[TransferItemIn] = Field(min_length=1)


class TransferItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty: float


class TransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    transfer_no: str
    from_location_id: int
    to_location_id: int
    status: str
    note: str | None = None
    items: list[TransferItemOut] = []


async def _out(db: AsyncSession, tr: StockTransfer) -> TransferOut:
    items = (
        await db.execute(
            select(StockTransferItem).where(StockTransferItem.transfer_id == tr.id)
        )
    ).scalars().all()
    out = TransferOut.model_validate(tr)
    out.items = [TransferItemOut.model_validate(i) for i in items]
    return out


async def _locked(db: AsyncSession, transfer_id: int) -> StockTransfer:
    tr = (
        await db.execute(
            select(StockTransfer).where(StockTransfer.id == transfer_id).with_for_update()
        )
    ).scalar_one_or_none()
    if tr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    return tr


@router.post("/transfers", response_model=TransferOut, operation_id="create_transfer")
async def create_transfer(
    data: TransferIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    tr = await transfers.create_transfer(
        db, data.from_location_id, data.to_location_id, data.items, data.note, user.id
    )
    await db.commit()
    await db.refresh(tr)
    return await _out(db, tr)


@router.get("/transfers", response_model=list[TransferOut], operation_id="list_transfers")
async def list_transfers(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(StockTransfer).order_by(StockTransfer.id.desc())
    if status_filter:
        q = q.where(StockTransfer.status == status_filter)
    rows = (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()
    if not rows:
        return []
    # Fetch all line items for the page in ONE query (no N+1 across transfers).
    items = (
        await db.execute(
            select(StockTransferItem).where(
                StockTransferItem.transfer_id.in_([t.id for t in rows])
            )
        )
    ).scalars().all()
    by_tr: dict[int, list] = {}
    for it in items:
        by_tr.setdefault(it.transfer_id, []).append(it)
    out = []
    for tr in rows:
        o = TransferOut.model_validate(tr)
        o.items = [TransferItemOut.model_validate(i) for i in by_tr.get(tr.id, [])]
        out.append(o)
    return out


@router.get("/transfers/{transfer_id}", response_model=TransferOut, operation_id="get_transfer")
async def get_transfer(transfer_id: int, db: AsyncSession = Depends(get_db)):
    tr = await db.get(StockTransfer, transfer_id)
    if tr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    return await _out(db, tr)


@router.post("/transfers/{transfer_id}/dispatch", response_model=TransferOut, operation_id="dispatch_transfer")
async def dispatch_transfer(
    transfer_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    tr = await _locked(db, transfer_id)
    await transfers.dispatch_transfer(db, tr, user.id)
    await db.commit()
    await db.refresh(tr)
    return await _out(db, tr)


@router.post("/transfers/{transfer_id}/receive", response_model=TransferOut, operation_id="receive_transfer")
async def receive_transfer(
    transfer_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    tr = await _locked(db, transfer_id)
    await transfers.receive_transfer(db, tr, user.id)
    await db.commit()
    await db.refresh(tr)
    return await _out(db, tr)


@router.post("/transfers/{transfer_id}/cancel", response_model=TransferOut, operation_id="cancel_transfer")
async def cancel_transfer(transfer_id: int, db: AsyncSession = Depends(get_db)):
    tr = await _locked(db, transfer_id)
    await transfers.cancel_transfer(db, tr)
    await db.commit()
    await db.refresh(tr)
    return await _out(db, tr)
