import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.idempotency import require_idempotency_key
from app.deps.pagination import Page, paged, pagination
from app.models.procurement import PurchaseOrder, PurchaseOrderItem, Supplier
from app.models.product import Product
from app.models.user import User
from app.services import procurement


async def _validate_products(db: AsyncSession, product_ids: list[int]) -> None:
    """422 (not a FK 500) if any referenced product doesn't exist."""
    wanted = set(product_ids)
    found = set(
        (await db.execute(select(Product.id).where(Product.id.in_(wanted)))).scalars().all()
    )
    missing = sorted(wanted - found)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown product_id(s): {missing}"
        )

router = APIRouter(
    prefix="/admin", tags=["procurement"], dependencies=[Depends(require_role("admin"))]
)


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    gstin: str | None = Field(default=None, max_length=15)
    contact_phone: str | None = Field(default=None, max_length=16)
    contact_email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=512)
    state: str | None = Field(default=None, max_length=120)


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    gstin: str | None = None
    state: str | None = None
    status: str


class POItemIn(BaseModel):
    product_id: int
    # qty x unit_cost must fit the Numeric(12,2) line/total columns (< 1e10).
    qty: float = Field(gt=0, le=100_000, allow_inf_nan=False)
    unit_cost: float = Field(ge=0, le=50_000, allow_inf_nan=False)


class POIn(BaseModel):
    supplier_id: int
    location_id: int | None = None
    expected_date: dt.date | None = None
    items: list[POItemIn] = Field(min_length=1, max_length=200)


class POItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty_ordered: float
    qty_received: float
    unit_cost: float
    line_total: float


class POOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    po_no: str
    supplier_id: int
    location_id: int
    status: str
    expected_date: dt.date | None = None
    subtotal: float
    total: float
    items: list[POItemOut] = []


class GRNLineIn(BaseModel):
    product_id: int
    qty_received: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    batch_no: str = Field(min_length=1, max_length=64)
    mfg_date: dt.date | None = None
    expiry_date: dt.date | None = None


class GRNIn(BaseModel):
    lines: list[GRNLineIn] = Field(min_length=1, max_length=200)


async def _po_out(db: AsyncSession, po: PurchaseOrder) -> POOut:
    items = (
        await db.execute(
            select(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == po.id)
        )
    ).scalars().all()
    out = POOut.model_validate(po)
    out.items = [POItemOut.model_validate(i) for i in items]
    return out


# ---------- Suppliers ----------
@router.post("/suppliers", response_model=SupplierOut, operation_id="create_supplier")
async def create_supplier(data: SupplierIn, db: AsyncSession = Depends(get_db)):
    s = Supplier(**data.model_dump())
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


@router.get("/suppliers", response_model=list[SupplierOut], operation_id="list_suppliers")
async def list_suppliers(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    return await paged(db, select(Supplier).order_by(Supplier.id), page)


# ---------- Purchase orders ----------
@router.post("/purchase-orders", response_model=POOut, operation_id="create_purchase_order")
async def create_po(
    data: POIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if await db.get(Supplier, data.supplier_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier not found")
    # Validate every product up front — an unknown product_id would otherwise FK-fail
    # the line insert and surface as a 500 instead of a clean 422.
    await _validate_products(db, [i.product_id for i in data.items])
    location_id = await procurement.resolve_location(db, data.location_id)
    po = await procurement.create_po(
        db, data.supplier_id, location_id, data.items, data.expected_date, user.id
    )
    await db.commit()
    await db.refresh(po)
    return await _po_out(db, po)


@router.get("/purchase-orders", response_model=list[POOut], operation_id="list_purchase_orders")
async def list_pos(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(PurchaseOrder).order_by(PurchaseOrder.id.desc())
    if status_filter:
        q = q.where(PurchaseOrder.status == status_filter)
    pos = await paged(db, q, page)
    if not pos:
        return []
    # Batch all line items in one query (no N+1 across the page).
    items = (
        await db.execute(
            select(PurchaseOrderItem).where(
                PurchaseOrderItem.purchase_order_id.in_([p.id for p in pos])
            )
        )
    ).scalars().all()
    by_po: dict[int, list[PurchaseOrderItem]] = {}
    for it in items:
        by_po.setdefault(it.purchase_order_id, []).append(it)
    out = []
    for po in pos:
        o = POOut.model_validate(po)
        o.items = [POItemOut.model_validate(i) for i in by_po.get(po.id, [])]
        out.append(o)
    return out


@router.get("/purchase-orders/{po_id}", response_model=POOut, operation_id="get_purchase_order")
async def get_po(po_id: int, db: AsyncSession = Depends(get_db)):
    po = await db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PO not found")
    return await _po_out(db, po)


async def _locked_po(db: AsyncSession, po_id: int) -> PurchaseOrder:
    po = (
        await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id).with_for_update())
    ).scalar_one_or_none()
    if po is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PO not found")
    return po


@router.post("/purchase-orders/{po_id}/place", response_model=POOut, operation_id="place_purchase_order")
async def place_po(po_id: int, db: AsyncSession = Depends(get_db)):
    po = await _locked_po(db, po_id)
    await procurement.place_po(db, po)
    await db.commit()
    await db.refresh(po)
    return await _po_out(db, po)


@router.post(
    "/purchase-orders/{po_id}/receive", response_model=POOut,
    operation_id="receive_purchase_order",
    dependencies=[Depends(require_idempotency_key)],
)
async def receive_po(
    po_id: int,
    data: GRNIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    po = await _locked_po(db, po_id)
    await procurement.receive_po(db, po, data.lines, user.id)
    await db.commit()
    await db.refresh(po)
    return await _po_out(db, po)


@router.post("/purchase-orders/{po_id}/cancel", response_model=POOut, operation_id="cancel_purchase_order")
async def cancel_po(po_id: int, db: AsyncSession = Depends(get_db)):
    po = await _locked_po(db, po_id)
    await procurement.cancel_po(db, po)
    await db.commit()
    await db.refresh(po)
    return await _po_out(db, po)
