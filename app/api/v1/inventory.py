from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.inventory import StockItem, StockMovement
from app.models.product import Product
from app.services import inventory

router = APIRouter(
    prefix="/admin/stock", tags=["inventory"], dependencies=[Depends(require_role("admin"))]
)


class StockAdjustIn(BaseModel):
    product_id: int
    change: float = Field(le=1_000_000, ge=-1_000_000, allow_inf_nan=False)
    reason: str = Field(pattern="^(receipt|adjustment|return)$")
    note: str | None = Field(default=None, max_length=255)


class WastageIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=255)


class StockItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty_on_hand: float


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    change: float
    reason: str
    note: str | None = None


async def _require_product(db: AsyncSession, product_id: int) -> None:
    if await db.get(Product, product_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")


@router.get("", response_model=list[StockItemOut], operation_id="list_stock")
async def list_stock(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    res = await db.execute(
        select(StockItem).order_by(StockItem.product_id).limit(page.limit).offset(page.offset)
    )
    return res.scalars().all()


@router.post("/adjust", response_model=StockItemOut, operation_id="adjust_stock")
async def adjust(data: StockAdjustIn, db: AsyncSession = Depends(get_db)):
    if data.change == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "change must be non-zero")
    await _require_product(db, data.product_id)
    item = await inventory.adjust_stock(
        db, data.product_id, data.change, data.reason, note=data.note
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.post("/wastage", response_model=StockItemOut, operation_id="record_wastage")
async def wastage(data: WastageIn, db: AsyncSession = Depends(get_db)):
    await _require_product(db, data.product_id)
    item = await inventory.adjust_stock(
        db, data.product_id, -data.qty, "wastage", note=data.note
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.get(
    "/{product_id}/movements",
    response_model=list[StockMovementOut],
    operation_id="list_stock_movements",
)
async def movements(
    product_id: int, db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)
):
    res = await db.execute(
        select(StockMovement)
        .where(StockMovement.product_id == product_id)
        .order_by(StockMovement.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    return res.scalars().all()
