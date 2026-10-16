"""Production-order lifecycle.

draft -> start (consume components FEFO at the location) -> complete (produce the
finished-goods batch at the location). Cancel is allowed before start only; once
components are issued the run must be completed. Component demand scales with
planned_qty / BOM.output_qty.
"""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockItem
from app.models.production import BillOfMaterials, BomComponent, ProductionOrder
from app.services import batches as batch_svc
from app.services import inventory


async def _components(db: AsyncSession, bom_id: int):
    return (
        await db.execute(select(BomComponent).where(BomComponent.bom_id == bom_id))
    ).scalars().all()


def _demand(component_qty: float, planned_qty: float, output_qty: float) -> float:
    return round(float(component_qty) * float(planned_qty) / float(output_qty), 3)


async def create_order(db: AsyncSession, bom_id, planned_qty, location_id, user_id) -> ProductionOrder:
    bom = await db.get(BillOfMaterials, bom_id)
    if bom is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "BOM not found")
    if bom.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "BOM is not active")
    if location_id is None:
        location_id = await inventory.default_location_id(db)
    pro = ProductionOrder(
        pro_no="PENDING",
        bom_id=bom.id,
        product_id=bom.product_id,
        location_id=location_id,
        planned_qty=planned_qty,
        status="draft",
        created_by=user_id,
    )
    db.add(pro)
    await db.flush()
    pro.pro_no = f"PR{pro.id:06d}"
    return pro


async def start_order(db: AsyncSession, pro: ProductionOrder, user_id) -> ProductionOrder:
    if pro.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Order is {pro.status}, cannot start")
    bom = await db.get(BillOfMaterials, pro.bom_id)
    components = await _components(db, pro.bom_id)
    if not components:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "BOM has no components")
    # Verify every component is available at the location before consuming any.
    needs = []
    for c in components:
        need = _demand(c.qty, pro.planned_qty, bom.output_qty)
        on_hand = await db.scalar(
            select(StockItem.qty_on_hand).where(
                StockItem.product_id == c.component_product_id,
                StockItem.location_id == pro.location_id,
            )
        )
        if on_hand is None or float(on_hand) < need:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Insufficient component {c.component_product_id} "
                f"(have {float(on_hand or 0)}, need {need})",
            )
        needs.append((c.component_product_id, need))
    for product_id, need in needs:
        await batch_svc.consume_fefo(
            db, product_id, need, "production",
            location_id=pro.location_id, ref_type="production", ref_id=pro.id,
            created_by=user_id,
        )
    pro.status = "in_progress"
    return pro


async def complete_order(db: AsyncSession, pro: ProductionOrder, output_batch_no, mfg_date, expiry_date, user_id) -> ProductionOrder:
    if pro.status != "in_progress":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Order is {pro.status}, cannot complete")
    await batch_svc.receive_batch(
        db, pro.product_id, output_batch_no, float(pro.planned_qty),
        location_id=pro.location_id, mfg_date=mfg_date, expiry_date=expiry_date,
        created_by=user_id,
    )
    pro.output_batch_no = output_batch_no
    pro.mfg_date = mfg_date
    pro.expiry_date = expiry_date
    pro.status = "completed"
    return pro


async def cancel_order(db: AsyncSession, pro: ProductionOrder) -> ProductionOrder:
    if pro.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Order is {pro.status}; only draft orders can be cancelled "
            "(components already issued)",
        )
    pro.status = "cancelled"
    return pro
