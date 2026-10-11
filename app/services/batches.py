"""Batch/lot receiving, FEFO consumption, and expiry write-off.

stock_items.qty_on_hand stays the aggregate; batches track the lot-level detail.
Consumption picks earliest-expiry-first (FEFO). If demand exceeds batched stock,
the remainder is still decremented from the aggregate as an unbatched movement
(stock can go negative — a delivery is never blocked on inventory).
"""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.batch import StockBatch
from app.models.inventory import StockMovement
from app.services.inventory import _locked_item, default_location_id


async def receive_batch(
    db: AsyncSession,
    product_id: int,
    batch_no: str,
    qty: float,
    *,
    location_id: int | None = None,
    mfg_date: dt.date | None = None,
    expiry_date: dt.date | None = None,
    created_by: int | None = None,
) -> StockBatch:
    if location_id is None:
        location_id = await default_location_id(db)
    batch = StockBatch(
        product_id=product_id,
        location_id=location_id,
        batch_no=batch_no,
        mfg_date=mfg_date,
        expiry_date=expiry_date,
        qty=qty,
    )
    db.add(batch)
    await db.flush()
    item = await _locked_item(db, product_id, location_id)
    item.qty_on_hand = round(float(item.qty_on_hand) + qty, 3)
    db.add(
        StockMovement(
            product_id=product_id, location_id=location_id, batch_id=batch.id,
            change=qty, reason="receipt", ref_type="batch", ref_id=batch.id, created_by=created_by,
        )
    )
    return batch


async def consume_fefo(
    db: AsyncSession,
    product_id: int,
    qty: float,
    reason: str,
    *,
    location_id: int | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    created_by: int | None = None,
) -> None:
    """Remove `qty` from stock, allocating across batches earliest-expiry-first."""
    if location_id is None:
        location_id = await default_location_id(db)
    item = await _locked_item(db, product_id, location_id)
    item.qty_on_hand = round(float(item.qty_on_hand) - qty, 3)

    batches = (
        await db.execute(
            select(StockBatch)
            .where(
                StockBatch.product_id == product_id,
                StockBatch.location_id == location_id,
                StockBatch.status == "active",
                StockBatch.qty > 0,
            )
            .order_by(StockBatch.expiry_date.asc().nulls_last(), StockBatch.id.asc())
            .with_for_update()
        )
    ).scalars().all()

    remaining = qty
    for b in batches:
        if remaining <= 0:
            break
        take = min(float(b.qty), remaining)
        b.qty = round(float(b.qty) - take, 3)
        if b.qty == 0:
            b.status = "depleted"
        remaining = round(remaining - take, 3)
        db.add(
            StockMovement(
                product_id=product_id, location_id=location_id, batch_id=b.id,
                change=-take, reason=reason, ref_type=ref_type, ref_id=ref_id, created_by=created_by,
            )
        )
    if remaining > 0:  # more demanded than batched stock — unbatched remainder
        db.add(
            StockMovement(
                product_id=product_id, location_id=location_id, batch_id=None,
                change=-remaining, reason=reason, ref_type=ref_type, ref_id=ref_id,
                created_by=created_by,
            )
        )


async def expire_batches(db: AsyncSession, as_of: dt.date, *, created_by: int | None = None) -> dict:
    """Write off batches whose expiry_date has passed: qty->0, wastage movement."""
    batches = (
        await db.execute(
            select(StockBatch)
            .where(
                StockBatch.status == "active",
                StockBatch.qty > 0,
                StockBatch.expiry_date.is_not(None),
                StockBatch.expiry_date < as_of,
            )
            .with_for_update()
        )
    ).scalars().all()
    count = 0
    total = 0.0
    for b in batches:
        qty = float(b.qty)
        item = await _locked_item(db, b.product_id, b.location_id)
        item.qty_on_hand = round(float(item.qty_on_hand) - qty, 3)
        b.qty = 0
        b.status = "expired"
        db.add(
            StockMovement(
                product_id=b.product_id, location_id=b.location_id, batch_id=b.id,
                change=-qty, reason="wastage", ref_type="expiry", ref_id=b.id,
                note="expired", created_by=created_by,
            )
        )
        count += 1
        total = round(total + qty, 3)
    return {"batches_expired": count, "qty_written_off": total}
