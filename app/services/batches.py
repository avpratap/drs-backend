"""Batch/lot receiving, FEFO consumption, and expiry write-off.

stock_items.qty_on_hand stays the aggregate; batches track the lot-level detail.
Consumption picks earliest-expiry-first (FEFO). If demand exceeds batched stock,
the remainder is still decremented from the aggregate as an unbatched movement
(stock can go negative — a delivery is never blocked on inventory).
"""
import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
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
    # Lock order is ALWAYS stock_item -> batch (same as consume_fefo / expiry / transfer
    # dispatch): taking the batch first here deadlocked against a concurrent delivery.
    item = await _locked_item(db, product_id, location_id)
    # Batch identity is (product, location, batch_no, expiry) — the SAME physical lot
    # received twice (two GRNs, a re-stock, a transfer-in of an existing lot) must MERGE
    # into one row, not create a duplicate that fragments FEFO and blurs lot identity (#26).
    dup_q = (
        select(StockBatch)
        .where(
            StockBatch.product_id == product_id,
            StockBatch.location_id == location_id,
            StockBatch.batch_no == batch_no,
            StockBatch.status == "active",
            StockBatch.expiry_date.is_(None)
            if expiry_date is None
            else StockBatch.expiry_date == expiry_date,
        )
        .with_for_update()
    )
    def _merge(b: StockBatch) -> None:
        b.qty = round(float(b.qty) + qty, 3)
        if b.mfg_date is None and mfg_date is not None:
            b.mfg_date = mfg_date

    batch = (await db.execute(dup_q)).scalars().first()
    if batch is not None:
        _merge(batch)
        await db.flush()
    else:
        # No existing lot: FOR UPDATE on zero rows takes no gap lock, so two concurrent
        # first-receives both reach here. The partial unique index
        # uq_stock_batch_active_identity (migration 0051) makes the second INSERT collide;
        # catch it in a savepoint and merge into the row the winner committed.
        try:
            async with db.begin_nested():
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
        except IntegrityError:
            batch = (await db.execute(dup_q)).scalars().one()
            _merge(batch)
            await db.flush()
    item.qty_on_hand = round(float(item.qty_on_hand) + qty, 3)
    db.add(
        StockMovement(
            product_id=product_id, location_id=location_id, batch_id=batch.id,
            change=qty, reason=reason, ref_type=ref_type,
            ref_id=ref_id if ref_id is not None else batch.id, created_by=created_by,
        )
    )
    return batch


class InsufficientStock(Exception):
    def __init__(self, available: float):
        super().__init__(f"only {available} available")
        self.available = available


async def _expire_unswept(
    db: AsyncSession, item, product_id: int, location_id: int, as_of: dt.date,
    created_by: int | None = None,
) -> tuple[int, float]:
    """Write off this (product, location)'s lots whose expiry is before `as_of`. Caller
    holds the StockItem lock (stock_item -> batch order). Returns (lots, qty)."""
    lots = (
        await db.execute(
            select(StockBatch)
            .where(
                StockBatch.product_id == product_id,
                StockBatch.location_id == location_id,
                StockBatch.status == "active",
                StockBatch.qty > 0,
                StockBatch.expiry_date.is_not(None),
                StockBatch.expiry_date < as_of,
            )
            .order_by(StockBatch.id)
            .with_for_update()
        )
    ).scalars().all()
    total = 0.0
    for b in lots:
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
        total = round(total + qty, 3)
    return len(lots), total


def _today() -> dt.date:
    return dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date()


async def usable_qty(db: AsyncSession, product_id: int, location_id: int) -> float:
    """On-hand minus lots already past expiry (not yet swept): what can really be used."""
    from app.models.inventory import StockItem

    on_hand = float(
        await db.scalar(
            select(StockItem.qty_on_hand).where(
                StockItem.product_id == product_id, StockItem.location_id == location_id
            )
        )
        or 0
    )
    expired = float(
        await db.scalar(
            select(func.coalesce(func.sum(StockBatch.qty), 0)).where(
                StockBatch.product_id == product_id,
                StockBatch.location_id == location_id,
                StockBatch.status == "active",
                StockBatch.expiry_date.is_not(None),
                StockBatch.expiry_date < _today(),
            )
        )
        or 0
    )
    return round(on_hand - expired, 3)


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
    note: str | None = None,
    strict: bool = False,
) -> None:
    """Remove `qty` from stock, allocating across batches earliest-expiry-first.

    Lots already past expiry (pending the sweep) are WRITTEN OFF first, under the same
    lock: previously they were skipped but kept their qty in on-hand, so the shortfall
    was booked unbatched and the later sweep removed the same units again (negative
    stock). Any remaining shortfall of fresh stock falls to the unbatched remainder
    (a delivery is never blocked), unless `strict` — then InsufficientStock is raised
    before anything changes (wastage / count corrections can't go below zero)."""
    location_id = await require_location(db, location_id)
    today = _today()
    item = await _locked_item(db, product_id, location_id)
    await _expire_unswept(db, item, product_id, location_id, today, created_by)
    if strict and float(item.qty_on_hand) < qty:
        raise InsufficientStock(max(float(item.qty_on_hand), 0.0))
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
                change=-take, reason=reason, ref_type=ref_type, ref_id=ref_id,
                note=note, created_by=created_by,
            )
        )
    if remaining > 0:  # more demanded than batched stock — unbatched remainder
        db.add(
            StockMovement(
                product_id=product_id, location_id=location_id, batch_id=None,
                change=-remaining, reason=reason, ref_type=ref_type, ref_id=ref_id,
                note=note, created_by=created_by,
            )
        )


async def expire_batches(db: AsyncSession, as_of: dt.date, *, created_by: int | None = None) -> dict:
    """Write off batches whose expiry_date has passed: qty->0, wastage movement.

    Locks the StockItem row BEFORE its batches — the same stock_item->batch order used
    by consume_fefo / transfers / production / receive — so concurrent jobs can't
    deadlock by acquiring the two rows in opposite orders.
    """
    groups = (
        await db.execute(
            select(StockBatch.product_id, StockBatch.location_id)
            .where(
                StockBatch.status == "active",
                StockBatch.qty > 0,
                StockBatch.expiry_date.is_not(None),
                StockBatch.expiry_date < as_of,
            )
            .distinct()
            .order_by(StockBatch.product_id, StockBatch.location_id)
        )
    ).all()
    count = 0
    total = 0.0
    for product_id, location_id in groups:
        item = await _locked_item(db, product_id, location_id)  # stock_item first
        n, q = await _expire_unswept(db, item, product_id, location_id, as_of, created_by)
        count += n
        total = round(total + q, 3)
    return {"batches_expired": count, "qty_written_off": total}
