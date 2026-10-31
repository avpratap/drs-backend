import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, paged, pagination
from app.models.product import Product
from app.models.production import BillOfMaterials, BomComponent, ProductionOrder
from app.models.user import User
from app.services import production

router = APIRouter(
    prefix="/admin", tags=["production"], dependencies=[Depends(require_role("admin"))]
)


# ---------- BOM schemas ----------
class BomComponentIn(BaseModel):
    component_product_id: int
    qty: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)


class BomIn(BaseModel):
    product_id: int
    name: str = Field(min_length=1, max_length=120)
    output_qty: float = Field(default=1, gt=0, le=1_000_000, allow_inf_nan=False)
    components: list[BomComponentIn] = Field(min_length=1, max_length=200)


class BomComponentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    component_product_id: int
    qty: float


class BomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    name: str
    output_qty: float
    status: str
    components: list[BomComponentOut] = []


# ---------- Production-order schemas ----------
class ProductionIn(BaseModel):
    bom_id: int
    planned_qty: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    location_id: int | None = None


class CompleteIn(BaseModel):
    output_batch_no: str = Field(min_length=1, max_length=64)
    # What the run actually yielded; omitted -> the planned quantity.
    produced_qty: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    mfg_date: dt.date | None = None
    expiry_date: dt.date | None = None

    @model_validator(mode="after")
    def _dates(self):
        if self.mfg_date and self.expiry_date and self.expiry_date < self.mfg_date:
            raise ValueError("expiry_date before mfg_date")  # same rule as receive_batch
        return self


class ProductionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    pro_no: str
    bom_id: int
    product_id: int
    location_id: int
    planned_qty: float
    status: str
    output_batch_no: str | None = None
    produced_qty: float | None = None


async def _bom_out(db: AsyncSession, bom: BillOfMaterials) -> BomOut:
    comps = (
        await db.execute(select(BomComponent).where(BomComponent.bom_id == bom.id))
    ).scalars().all()
    out = BomOut.model_validate(bom)
    out.components = [BomComponentOut.model_validate(c) for c in comps]
    return out


async def _require_product(db: AsyncSession, product_id: int) -> None:
    if await db.get(Product, product_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Product {product_id} not found")


# ---------- BOM endpoints ----------
@router.post("/boms", response_model=BomOut, operation_id="create_bom")
async def create_bom(data: BomIn, db: AsyncSession = Depends(get_db)):
    await _require_product(db, data.product_id)
    for c in data.components:
        await _require_product(db, c.component_product_id)
        if c.component_product_id == data.product_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "A product cannot be its own component"
            )
    bom = BillOfMaterials(product_id=data.product_id, name=data.name, output_qty=data.output_qty)
    db.add(bom)
    await db.flush()
    for c in data.components:
        db.add(BomComponent(bom_id=bom.id, component_product_id=c.component_product_id, qty=c.qty))
    await db.commit()
    await db.refresh(bom)
    return await _bom_out(db, bom)


@router.get("/boms", response_model=list[BomOut], operation_id="list_boms")
async def list_boms(
    product_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(BillOfMaterials).order_by(BillOfMaterials.id.desc())
    if product_id is not None:
        q = q.where(BillOfMaterials.product_id == product_id)
    boms = await paged(db, q, page)
    if not boms:
        return []
    # Batch all components in one query (no N+1 across the page).
    comps = (
        await db.execute(
            select(BomComponent).where(BomComponent.bom_id.in_([b.id for b in boms]))
        )
    ).scalars().all()
    by_bom: dict[int, list[BomComponent]] = {}
    for c in comps:
        by_bom.setdefault(c.bom_id, []).append(c)
    out = []
    for b in boms:
        o = BomOut.model_validate(b)
        o.components = [BomComponentOut.model_validate(c) for c in by_bom.get(b.id, [])]
        out.append(o)
    return out


@router.get("/boms/{bom_id}", response_model=BomOut, operation_id="get_bom")
async def get_bom(bom_id: int, db: AsyncSession = Depends(get_db)):
    bom = await db.get(BillOfMaterials, bom_id)
    if bom is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "BOM not found")
    return await _bom_out(db, bom)


# ---------- Production-order endpoints ----------
async def _locked(db: AsyncSession, pro_id: int) -> ProductionOrder:
    pro = (
        await db.execute(
            select(ProductionOrder).where(ProductionOrder.id == pro_id).with_for_update()
        )
    ).scalar_one_or_none()
    if pro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Production order not found")
    return pro


@router.post("/production-orders", response_model=ProductionOut, operation_id="create_production_order")
async def create_production_order(
    data: ProductionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    pro = await production.create_order(db, data.bom_id, data.planned_qty, data.location_id, user.id)
    await db.commit()
    await db.refresh(pro)
    return pro


@router.get("/production-orders", response_model=list[ProductionOut], operation_id="list_production_orders")
async def list_production_orders(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(ProductionOrder).order_by(ProductionOrder.id.desc())
    if status_filter:
        q = q.where(ProductionOrder.status == status_filter)
    return await paged(db, q, page)


@router.get("/production-orders/{pro_id}", response_model=ProductionOut, operation_id="get_production_order")
async def get_production_order(pro_id: int, db: AsyncSession = Depends(get_db)):
    pro = await db.get(ProductionOrder, pro_id)
    if pro is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Production order not found")
    return pro


@router.post("/production-orders/{pro_id}/start", response_model=ProductionOut, operation_id="start_production_order")
async def start_production_order(
    pro_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    pro = await _locked(db, pro_id)
    await production.start_order(db, pro, user.id)
    await db.commit()
    await db.refresh(pro)
    return pro


@router.post("/production-orders/{pro_id}/complete", response_model=ProductionOut, operation_id="complete_production_order")
async def complete_production_order(
    pro_id: int,
    data: CompleteIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pro = await _locked(db, pro_id)
    await production.complete_order(
        db, pro, data.output_batch_no, data.mfg_date, data.expiry_date, user.id,
        produced_qty=data.produced_qty,
    )
    await db.commit()
    await db.refresh(pro)
    return pro


@router.post("/production-orders/{pro_id}/cancel", response_model=ProductionOut, operation_id="cancel_production_order")
async def cancel_production_order(pro_id: int, db: AsyncSession = Depends(get_db)):
    pro = await _locked(db, pro_id)
    await production.cancel_order(db, pro)
    await db.commit()
    await db.refresh(pro)
    return pro
