"""Stock adjustments: lock the stock row, update on-hand, append a movement."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockItem, StockMovement


async def _locked_item(db: AsyncSession, product_id: int) -> StockItem:
    res = await db.execute(
        select(StockItem).where(StockItem.product_id == product_id).with_for_update()
    )
    item = res.scalar_one_or_none()
    if item is None:
        item = StockItem(product_id=product_id, qty_on_hand=0)
        db.add(item)
        await db.flush()
        res = await db.execute(
            select(StockItem).where(StockItem.id == item.id).with_for_update()
        )
        item = res.scalar_one()
    return item


async def adjust_stock(
    db: AsyncSession,
    product_id: int,
    change: float,
    reason: str,
    *,
    ref_type: str | None = None,
    ref_id: int | None = None,
    note: str | None = None,
    created_by: int | None = None,
) -> StockItem:
    item = await _locked_item(db, product_id)
    item.qty_on_hand = round(float(item.qty_on_hand) + change, 3)
    db.add(
        StockMovement(
            product_id=product_id,
            change=change,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
            note=note,
            created_by=created_by,
        )
    )
    return item
