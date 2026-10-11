from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.inventory import StockItem, StockMovement
from app.models.product import Product
from app.models.warehouse import Location
from app.services import inventory

router = APIRouter(
    prefix="/admin", tags=["inventory"], dependencies=[Depends(require_role("admin"))]
)


# ---------- Schemas ----------
class LocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="warehouse", pattern="^(plant|hub|cold_storage|vehicle|store|warehouse)$")
    state: str | None = Field(default=None, max_length=120)
    is_default: bool = False


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    kind: str
    state: str | None = None
    is_default: bool
    status: str


class StockAdjustIn(BaseModel):
    product_id: int
    change: float = Field(le=1_000_000, ge=-1_000_000, allow_inf_nan=False)
    reason: str = Field(pattern="^(receipt|adjustment|return)$")
    location_id: int | None = None
    note: str | None = Field(default=None, max_length=255)


class WastageIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    location_id: int | None = None
    note: str | None = Field(default=None, max_length=255)


class ReorderIn(BaseModel):
    product_id: int
    reorder_point: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    location_id: int | None = None


class StockItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    location_id: int
    qty_on_hand: float
    reorder_point: float


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    location_id: int | None = None
    change: float
    reason: str
    note: str | None = None


# ---------- Locations ----------
@router.post("/locations", response_model=LocationOut, operation_id="create_location")
async def create_location(data: LocationIn, db: AsyncSession = Depends(get_db)):
    if data.is_default:  # only one default
        for loc in (await db.execute(select(Location).where(Location.is_default))).scalars():
            loc.is_default = False
    loc = Location(**data.model_dump())
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return loc


@router.get("/locations", response_model=list[LocationOut], operation_id="list_locations")
async def list_locations(db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(Location).order_by(Location.id))).scalars().all()


# ---------- Stock ----------
async def _require_product(db: AsyncSession, product_id: int) -> None:
    if await db.get(Product, product_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")


@router.get("/stock", response_model=list[StockItemOut], operation_id="list_stock")
async def list_stock(
    location_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(StockItem).order_by(StockItem.product_id)
    if location_id is not None:
        q = q.where(StockItem.location_id == location_id)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@router.get("/stock/low", response_model=list[StockItemOut], operation_id="list_low_stock")
async def list_low_stock(db: AsyncSession = Depends(get_db)):
    """Items at or below their reorder point (reorder_point > 0)."""
    q = select(StockItem).where(
        StockItem.reorder_point > 0, StockItem.qty_on_hand <= StockItem.reorder_point
    )
    return (await db.execute(q)).scalars().all()


@router.post("/stock/adjust", response_model=StockItemOut, operation_id="adjust_stock")
async def adjust(data: StockAdjustIn, db: AsyncSession = Depends(get_db)):
    if data.change == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "change must be non-zero")
    await _require_product(db, data.product_id)
    item = await inventory.adjust_stock(
        db, data.product_id, data.change, data.reason, location_id=data.location_id, note=data.note
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.post("/stock/wastage", response_model=StockItemOut, operation_id="record_wastage")
async def wastage(data: WastageIn, db: AsyncSession = Depends(get_db)):
    await _require_product(db, data.product_id)
    item = await inventory.adjust_stock(
        db, data.product_id, -data.qty, "wastage", location_id=data.location_id, note=data.note
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.post("/stock/reorder", response_model=StockItemOut, operation_id="set_reorder_point")
async def set_reorder(data: ReorderIn, db: AsyncSession = Depends(get_db)):
    await _require_product(db, data.product_id)
    loc = data.location_id or await inventory.default_location_id(db)
    item = await db.scalar(
        select(StockItem).where(
            StockItem.product_id == data.product_id, StockItem.location_id == loc
        )
    )
    if item is None:
        item = StockItem(product_id=data.product_id, location_id=loc, qty_on_hand=0)
        db.add(item)
    item.reorder_point = data.reorder_point
    await db.commit()
    await db.refresh(item)
    return item


@router.get(
    "/stock/{product_id}/movements",
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
