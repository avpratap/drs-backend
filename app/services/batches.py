"""Batch/lot receiving, FEFO consumption, and expiry write-off.

stock_items.qty_on_hand stays the aggregate; batches track the lot-level detail.
Consumption picks earliest-expiry-first (FEFO). If demand exceeds batched stock,
the remainder is still decremented from the aggregate as an unbatched movement
(stock can go negative — a delivery is never blocked on inventory).
"""
import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.batch import StockBatch
from app.models.inventory import StockMovement
from app.services.inventory import _locked_item, require_location


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
    reason: str = "receipt",
    ref_type: str = "batch",
    ref_id: int | None = None,
) -> StockBatch:
    """Receive a lot. `reason`/`ref_type`/`ref_id` label the movement (default is a
    plain receipt referencing the batch; callers like transfers pass transfer_in)."""
    # Central guard so EVERY receive path (GRN, production, transfer) rejects a lot whose
    # expiry precedes manufacture — such a batch would poison FEFO (sorts first) / be
    # wrongly written off.
    if mfg_date and expiry_date and expiry_date < mfg_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "expiry_date cannot be before mfg_date"
        )
    location_id = await require_location(db, location_id)
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
            change=qty, reason=reason, ref_type=ref_type,
            ref_id=ref_id if ref_id is not None else batch.id, created_by=created_by,
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
    """Remove `qty` from stock, allocating across batches earliest-expiry-first.

    Already-expired lots (expiry < today, pending the expiry sweep) are NOT allocated, so
    FEFO never ships expired perishables; any shortfall of fresh stock falls to the
    unbatched remainder (aggregate may go negative — a delivery is never blocked)."""
    location_id = await require_location(db, location_id)
    today = dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date()
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
                or_(StockBatch.expiry_date.is_(None), StockBatch.expiry_date >= today),
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
    """Write off batches whose expiry_date has passed: qty->0, wastage movement.

    Locks the StockItem row BEFORE its batches — the same stock_item->batch order used
    by consume_fefo / transfers / production — so a concurrent delivery and this expiry
    job can't deadlock by acquiring the two rows in opposite orders.
    """
    _expired = (
        StockBatch.status == "active",
        StockBatch.qty > 0,
        StockBatch.expiry_date.is_not(None),
        StockBatch.expiry_date < as_of,
    )
    groups = (
        await db.execute(
            select(StockBatch.product_id, StockBatch.location_id)
            .where(*_expired)
            .distinct()
            # Deterministic lock order (product, location) — matches the stock_item->batch
            # lock order used elsewhere so concurrent jobs can't deadlock.
            .order_by(StockBatch.product_id, StockBatch.location_id)
        )
    ).all()
    count = 0
    total = 0.0
    for product_id, location_id in groups:
        item = await _locked_item(db, product_id, location_id)  # stock_item first
        batches = (
            await db.execute(
                select(StockBatch)
                .where(
                    StockBatch.product_id == product_id,
                    StockBatch.location_id == location_id,
                    *_expired,
                )
                .with_for_update()
            )
        ).scalars().all()
        for b in batches:
            qty = float(b.qty)
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
