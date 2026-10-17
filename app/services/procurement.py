"""Purchase-order lifecycle + goods receipt (GRN -> stock batches)."""
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.procurement import PurchaseOrder, PurchaseOrderItem
from app.models.warehouse import Location
from app.services import batches as batch_svc
from app.services.inventory import default_location_id


async def create_po(db: AsyncSession, supplier_id: int, location_id, items, expected_date, user_id):
    if location_id is None:
        location_id = await default_location_id(db)
    po = PurchaseOrder(
        po_no=f"PENDING-{uuid4().hex[:20]}",
        supplier_id=supplier_id,
        location_id=location_id,
        status="draft",
        expected_date=expected_date,
        created_by=user_id,
    )
    db.add(po)
    await db.flush()
    po.po_no = f"PO{po.id:06d}"
    # Reject duplicate product lines — the receive path keys items by product_id, so two
    # lines for one product would otherwise collapse (one silently never received).
    seen_products: set[int] = set()
    subtotal = 0.0
    for it in items:
        if it.product_id in seen_products:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Product {it.product_id} appears more than once; combine into one line",
            )
        seen_products.add(it.product_id)
        line = round(float(it.unit_cost) * float(it.qty), 2)
        subtotal += line
        db.add(
            PurchaseOrderItem(
                purchase_order_id=po.id,
                product_id=it.product_id,
                qty_ordered=it.qty,
                unit_cost=it.unit_cost,
                line_total=line,
            )
        )
    po.subtotal = round(subtotal, 2)
    po.total = round(subtotal, 2)
    return po


async def place_po(db: AsyncSession, po: PurchaseOrder) -> None:
    if po.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, f"PO is {po.status}, cannot place")
    po.status = "placed"


async def cancel_po(db: AsyncSession, po: PurchaseOrder) -> None:
    if po.status not in ("draft", "placed"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"PO is {po.status}, cannot cancel")
    po.status = "cancelled"


async def receive_po(db: AsyncSession, po: PurchaseOrder, lines, user_id) -> PurchaseOrder:
    """GRN: receive per-line quantities as stock batches; advance PO status."""
    if po.status not in ("placed", "partially_received"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"PO is {po.status}, cannot receive")
    po_items = {
        i.product_id: i
        for i in (
            await db.execute(
                select(PurchaseOrderItem)
                .where(PurchaseOrderItem.purchase_order_id == po.id)
                .with_for_update()
            )
        ).scalars().all()
    }
    for ln in lines:
        item = po_items.get(ln.product_id)
        if item is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Product {ln.product_id} not on this PO"
            )
        new_received = round(float(item.qty_received) + float(ln.qty_received), 3)
        if new_received > float(item.qty_ordered):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Over-receipt for product {ln.product_id} "
                f"({new_received} > ordered {float(item.qty_ordered)})",
            )
        item.qty_received = new_received
        await batch_svc.receive_batch(
            db, ln.product_id, ln.batch_no, float(ln.qty_received),
            location_id=po.location_id, mfg_date=ln.mfg_date, expiry_date=ln.expiry_date,
            created_by=user_id, reason="receipt", ref_type="purchase_order", ref_id=po.id,
        )
    fully = all(
        float(i.qty_received) >= float(i.qty_ordered) for i in po_items.values()
    )
    po.status = "received" if fully else "partially_received"
    return po


async def resolve_location(db: AsyncSession, location_id: int | None) -> int | None:
    if location_id is None:
        return await default_location_id(db)
    if await db.get(Location, location_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Location not found")
    return location_id
