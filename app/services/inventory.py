"""Stock adjustments: lock the (product, location) row, update on-hand, append a
movement. Location-aware; a default location is used when none is given."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockItem, StockMovement
from app.models.warehouse import Location


async def default_location_id(db: AsyncSession) -> int | None:
    lid = await db.scalar(select(Location.id).where(Location.is_default).limit(1))
    if lid is None:
        lid = await db.scalar(select(Location.id).order_by(Location.id).limit(1))
    return lid


async def _locked_item(db: AsyncSession, product_id: int, location_id: int) -> StockItem:
    res = await db.execute(
        select(StockItem)
        .where(StockItem.product_id == product_id, StockItem.location_id == location_id)
        .with_for_update()
    )
    item = res.scalar_one_or_none()
    if item is None:
        item = StockItem(product_id=product_id, location_id=location_id, qty_on_hand=0)
        db.add(item)
        await db.flush()
        item = (
            await db.execute(select(StockItem).where(StockItem.id == item.id).with_for_update())
        ).scalar_one()
    return item


async def adjust_stock(
    db: AsyncSession,
    product_id: int,
    change: float,
    reason: str,
    *,
    location_id: int | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    batch_id: int | None = None,
    note: str | None = None,
    created_by: int | None = None,
) -> StockItem:
    if location_id is None:
        location_id = await default_location_id(db)
    item = await _locked_item(db, product_id, location_id)
    item.qty_on_hand = round(float(item.qty_on_hand) + change, 3)
    db.add(
        StockMovement(
            product_id=product_id,
            location_id=location_id,
            batch_id=batch_id,
            change=change,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
            note=note,
            created_by=created_by,
        )
    )
    return item
