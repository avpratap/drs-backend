"""Stock adjustments: lock the (product, location) row, update on-hand, append a
movement. Location-aware; a default location is used when none is given."""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockItem, StockMovement
from app.models.warehouse import Location


async def default_location_id(db: AsyncSession) -> int | None:
    lid = await db.scalar(select(Location.id).where(Location.is_default).limit(1))
    if lid is None:
        lid = await db.scalar(select(Location.id).order_by(Location.id).limit(1))
    return lid


async def delivery_location_id(db: AsyncSession, shop_id: int, actor_id: int | None) -> int | None:
    """Where a delivery's stock comes from: the delivering driver's vehicle; else (an
    admin completing it) the vehicle of the driver assigned to the shop; else default."""
    from app.models.shop import DeliveryAssignment

    candidates = [actor_id] if actor_id is not None else []
    candidates += list(
        (
            await db.execute(
                select(DeliveryAssignment.delivery_id)
                .where(DeliveryAssignment.shop_id == shop_id, DeliveryAssignment.active.is_(True))
                .order_by(DeliveryAssignment.id)
            )
        ).scalars()
    )
    for uid in candidates:
        lid = await db.scalar(
            select(Location.id)
            .where(
                Location.driver_user_id == uid,
                Location.kind == "vehicle",
                Location.status == "active",
            )
            .order_by(Location.id)
            .limit(1)
        )
        if lid is not None:
            return lid
    return await default_location_id(db)


async def require_location(db: AsyncSession, location_id: int | None) -> int:
    """Resolve the location (given or default); 409 if none is configured — so a
    missing location fails cleanly instead of a NOT-NULL IntegrityError 500 later."""
    if location_id is None:
        location_id = await default_location_id(db)
        if location_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "No stock location configured")
        return location_id
    # A supplied id must actually exist, else inserts fail with an opaque FK 500.
    if await db.get(Location, location_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Location not found")
    return location_id


async def _select_item_locked(db: AsyncSession, product_id: int, location_id: int) -> StockItem | None:
    return (
        await db.execute(
            select(StockItem)
            .where(StockItem.product_id == product_id, StockItem.location_id == location_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _locked_item(db: AsyncSession, product_id: int, location_id: int) -> StockItem:
    item = await _select_item_locked(db, product_id, location_id)
    if item is not None:
        return item
    # Create under a SAVEPOINT; a concurrent first-touch may win the unique
    # (product_id, location_id) index — on conflict, re-select the now-committed row
    # (locked) instead of crashing with an IntegrityError 500.
    try:
        async with db.begin_nested():
            item = StockItem(product_id=product_id, location_id=location_id, qty_on_hand=0)
            db.add(item)
            await db.flush()
    except IntegrityError:
        item = await _select_item_locked(db, product_id, location_id)
        if item is None:  # extremely unlikely; surface cleanly
            raise
        return item
    return (
        await db.execute(select(StockItem).where(StockItem.id == item.id).with_for_update())
    ).scalar_one()


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
    location_id = await require_location(db, location_id)
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
