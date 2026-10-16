"""Stock-transfer lifecycle: draft -> dispatch (FEFO out of source) -> receive
(rebuild lots at destination). Cancel is allowed before dispatch only; once goods
are in transit they must be received at the destination.
"""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.batch import StockBatch
from app.models.transfer import StockTransfer, StockTransferItem, StockTransferLot
from app.models.warehouse import Location
from app.services import batches as batch_svc
from app.services import inventory


async def _loc_or_404(db: AsyncSession, location_id: int) -> int:
    if await db.get(Location, location_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Location {location_id} not found")
    return location_id


async def create_transfer(db: AsyncSession, from_loc, to_loc, items, note, user_id) -> StockTransfer:
    if from_loc is None:
        from_loc = await inventory.default_location_id(db)
    await _loc_or_404(db, from_loc)
    await _loc_or_404(db, to_loc)
    if from_loc == to_loc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Source and destination must differ")
    tr = StockTransfer(
        transfer_no="PENDING",
        from_location_id=from_loc,
        to_location_id=to_loc,
        status="draft",
        note=note,
        created_by=user_id,
    )
    db.add(tr)
    await db.flush()
    tr.transfer_no = f"TR{tr.id:06d}"
    for it in items:
        db.add(StockTransferItem(transfer_id=tr.id, product_id=it.product_id, qty=it.qty))
    return tr


async def _items(db: AsyncSession, transfer_id: int, *, lock: bool = False):
    q = select(StockTransferItem).where(StockTransferItem.transfer_id == transfer_id)
    if lock:
        q = q.with_for_update()
    return (await db.execute(q)).scalars().all()


async def dispatch_transfer(db: AsyncSession, tr: StockTransfer, user_id) -> StockTransfer:
    if tr.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Transfer is {tr.status}, cannot dispatch")
    items = await _items(db, tr.id, lock=True)
    for it in items:
        qty = float(it.qty)
        item_row = await inventory._locked_item(db, it.product_id, tr.from_location_id)
        if float(item_row.qty_on_hand) < qty:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Insufficient stock for product {it.product_id} at source "
                f"(have {float(item_row.qty_on_hand)}, need {qty})",
            )
        # Allocate FEFO out of the source, snapshotting each lot that moves.
        src_batches = (
            await db.execute(
                select(StockBatch)
                .where(
                    StockBatch.product_id == it.product_id,
                    StockBatch.location_id == tr.from_location_id,
                    StockBatch.status == "active",
                    StockBatch.qty > 0,
                )
                .order_by(StockBatch.expiry_date.asc().nulls_last(), StockBatch.id.asc())
                .with_for_update()
            )
        ).scalars().all()
        remaining = qty
        item_row.qty_on_hand = round(float(item_row.qty_on_hand) - qty, 3)
        for b in src_batches:
            if remaining <= 0:
                break
            take = min(float(b.qty), remaining)
            b.qty = round(float(b.qty) - take, 3)
            if b.qty == 0:
                b.status = "depleted"
            remaining = round(remaining - take, 3)
            db.add(inventory.StockMovement(
                product_id=it.product_id, location_id=tr.from_location_id, batch_id=b.id,
                change=-take, reason="transfer_out", ref_type="transfer", ref_id=tr.id,
                created_by=user_id,
            ))
            db.add(StockTransferLot(
                transfer_item_id=it.id, batch_no=b.batch_no,
                mfg_date=b.mfg_date, expiry_date=b.expiry_date, qty=take,
            ))
        if remaining > 0:  # unbatched remainder leaves as an anonymous lot
            db.add(inventory.StockMovement(
                product_id=it.product_id, location_id=tr.from_location_id, batch_id=None,
                change=-remaining, reason="transfer_out", ref_type="transfer", ref_id=tr.id,
                created_by=user_id,
            ))
            db.add(StockTransferLot(transfer_item_id=it.id, batch_no=None, qty=remaining))
    tr.status = "in_transit"
    return tr


async def receive_transfer(db: AsyncSession, tr: StockTransfer, user_id) -> StockTransfer:
    if tr.status != "in_transit":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Transfer is {tr.status}, cannot receive")
    items = await _items(db, tr.id, lock=True)
    for it in items:
        lots = (
            await db.execute(
                select(StockTransferLot).where(StockTransferLot.transfer_item_id == it.id)
            )
        ).scalars().all()
        for lot in lots:
            if lot.batch_no is not None:
                await batch_svc.receive_batch(
                    db, it.product_id, lot.batch_no, float(lot.qty),
                    location_id=tr.to_location_id, mfg_date=lot.mfg_date,
                    expiry_date=lot.expiry_date, created_by=user_id,
                )
                # receive_batch logs reason="receipt"; relabel the destination arrival.
            else:
                await inventory.adjust_stock(
                    db, it.product_id, float(lot.qty), "transfer_in",
                    location_id=tr.to_location_id, ref_type="transfer", ref_id=tr.id,
                    created_by=user_id,
                )
    tr.status = "received"
    return tr


async def cancel_transfer(db: AsyncSession, tr: StockTransfer) -> StockTransfer:
    if tr.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Transfer is {tr.status}; only draft transfers can be cancelled "
            "(in-transit goods must be received)",
        )
    tr.status = "cancelled"
    return tr
