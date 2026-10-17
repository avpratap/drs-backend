import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, pagination
from app.models.batch import StockBatch
from app.models.inventory import StockItem, StockMovement
from app.models.product import Product
from app.models.user import User
from app.models.warehouse import Location
from app.services import batches as batch_svc
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
        # Serialize concurrent default-location creates (a fixed advisory lock) so two
        # requests can't each clear + set is_default and leave the system with two defaults.
        await db.execute(select(func.pg_advisory_xact_lock(0x10C0DEF)))
        for loc in (await db.execute(select(Location).where(Location.is_default))).scalars():
            loc.is_default = False
    loc = Location(**data.model_dump())
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return loc


@router.get("/locations", response_model=list[LocationOut], operation_id="list_locations")
async def list_locations(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    return (
        await db.execute(select(Location).order_by(Location.id).limit(page.limit).offset(page.offset))
    ).scalars().all()


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
async def list_low_stock(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    """Items at or below their reorder point (reorder_point > 0)."""
    q = select(StockItem).where(
        StockItem.reorder_point > 0, StockItem.qty_on_hand <= StockItem.reorder_point
    ).order_by(StockItem.product_id)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


class ReorderSuggestionOut(BaseModel):
    product_id: int
    location_id: int
    qty_on_hand: float
    reorder_point: float
    suggested_qty: float


@router.get(
    "/stock/reorder-suggestions",
    response_model=list[ReorderSuggestionOut],
    operation_id="list_reorder_suggestions",
)
async def list_reorder_suggestions(
    db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)
):
    """For each item at/below its reorder point, suggest a replenishment qty that
    tops it back up to 2x the reorder point (a starting point for a purchase order)."""
    rows = (
        await db.execute(
            select(StockItem)
            .where(
                StockItem.reorder_point > 0,
                StockItem.qty_on_hand <= StockItem.reorder_point,
            )
            .order_by(StockItem.product_id)
            .limit(page.limit)
            .offset(page.offset)
        )
    ).scalars().all()
    out = []
    for it in rows:
        target = float(it.reorder_point) * 2
        suggested = round(max(target - float(it.qty_on_hand), 0.0), 3)
        out.append(
            ReorderSuggestionOut(
                product_id=it.product_id,
                location_id=it.location_id,
                qty_on_hand=float(it.qty_on_hand),
                reorder_point=float(it.reorder_point),
                suggested_qty=suggested,
            )
        )
    return out


@router.post("/stock/adjust", response_model=StockItemOut, operation_id="adjust_stock")
async def adjust(
    data: StockAdjustIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if data.change == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "change must be non-zero")
    await _require_product(db, data.product_id)
    item = await inventory.adjust_stock(
        db, data.product_id, data.change, data.reason,
        location_id=data.location_id, note=data.note, created_by=user.id,
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.post("/stock/wastage", response_model=StockItemOut, operation_id="record_wastage")
async def wastage(
    data: WastageIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require_product(db, data.product_id)
    item = await inventory.adjust_stock(
        db, data.product_id, -data.qty, "wastage",
        location_id=data.location_id, note=data.note, created_by=user.id,
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.post("/stock/reorder", response_model=StockItemOut, operation_id="set_reorder_point")
async def set_reorder(data: ReorderIn, db: AsyncSession = Depends(get_db)):
    await _require_product(db, data.product_id)
    # Reuse the hardened helpers: require_location validates/defaults the location (clean
    # 404/409 instead of an FK/NOT-NULL 500) and _locked_item get-or-creates race-safely.
    loc = await inventory.require_location(db, data.location_id)
    item = await inventory._locked_item(db, data.product_id, loc)
    item.reorder_point = data.reorder_point
    await db.commit()
    await db.refresh(item)
    return item


class BatchIn(BaseModel):
    product_id: int
    batch_no: str = Field(min_length=1, max_length=64)
    qty: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    location_id: int | None = None
    mfg_date: dt.date | None = None
    expiry_date: dt.date | None = None


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    location_id: int
    batch_no: str
    mfg_date: dt.date | None = None
    expiry_date: dt.date | None = None
    qty: float
    status: str


@router.post("/stock/batches", response_model=BatchOut, operation_id="receive_batch")
async def receive_batch(
    data: BatchIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await _require_product(db, data.product_id)
    if data.expiry_date and data.mfg_date and data.expiry_date < data.mfg_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expiry before mfg date")
    batch = await batch_svc.receive_batch(
        db, data.product_id, data.batch_no, data.qty,
        location_id=data.location_id, mfg_date=data.mfg_date, expiry_date=data.expiry_date,
        created_by=user.id,
    )
    await db.commit()
    await db.refresh(batch)
    return batch


@router.get("/stock/batches", response_model=list[BatchOut], operation_id="list_batches")
async def list_batches(
    product_id: int | None = None,
    location_id: int | None = None,
    expiring_before: dt.date | None = None,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(StockBatch).order_by(
        StockBatch.expiry_date.asc().nulls_last(), StockBatch.id.asc()
    )
    if product_id is not None:
        q = q.where(StockBatch.product_id == product_id)
    if location_id is not None:
        q = q.where(StockBatch.location_id == location_id)
    if active_only:
        q = q.where(StockBatch.status == "active", StockBatch.qty > 0)
    if expiring_before is not None:
        q = q.where(
            StockBatch.expiry_date.is_not(None), StockBatch.expiry_date < expiring_before
        )
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


class ExpireResultOut(BaseModel):
    batches_expired: int
    qty_written_off: float


@router.post("/stock/expire", operation_id="expire_batches", response_model=ExpireResultOut)
async def expire_batches(
    as_of: dt.date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Write off batches past their expiry date (as_of defaults to today)."""
    result = await batch_svc.expire_batches(
        db, as_of or dt.datetime.now(dt.UTC).date(), created_by=user.id
    )
    await db.commit()
    return result


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
