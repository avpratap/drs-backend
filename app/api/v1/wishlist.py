"""Customer wishlist / save-for-later."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.models.product import Product
from app.models.user import User
from app.models.wishlist import WishlistItem
from app.services.orders import _get_or_create_account

router = APIRouter(prefix="/wishlist", tags=["wishlist"])


class WishlistIn(BaseModel):
    product_id: int


class WishlistItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int


@router.get("", response_model=list[WishlistItemOut], operation_id="list_wishlist")
async def list_wishlist(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    account = await _get_or_create_account(db, user)
    rows = (
        await db.execute(
            select(WishlistItem)
            .where(WishlistItem.account_id == account.id)
            .order_by(WishlistItem.id)
            .limit(500)
        )
    ).scalars().all()
    return rows


@router.post("", response_model=list[WishlistItemOut], operation_id="add_to_wishlist")
async def add_to_wishlist(
    data: WishlistIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    product = await db.get(Product, data.product_id)
    if product is None or product.status != "active":  # retired products aren't listable
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    account = await _get_or_create_account(db, user)
    existing = await db.scalar(
        select(WishlistItem).where(
            WishlistItem.account_id == account.id, WishlistItem.product_id == data.product_id
        )
    )
    if existing is None:  # idempotent add
        db.add(WishlistItem(account_id=account.id, product_id=data.product_id))
        try:
            await db.commit()
        except IntegrityError:
            # Concurrent add of the same product (uq_wishlist_account_product) — already
            # present, so treat as success.
            await db.rollback()
    else:
        await db.commit()
    rows = (
        await db.execute(
            select(WishlistItem)
            .where(WishlistItem.account_id == account.id)
            .order_by(WishlistItem.id)
            .limit(500)
        )
    ).scalars().all()
    return rows


@router.delete(
    "/{product_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="remove_from_wishlist"
)
async def remove_from_wishlist(
    product_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    account = await _get_or_create_account(db, user)
    row = await db.scalar(
        select(WishlistItem).where(
            WishlistItem.account_id == account.id, WishlistItem.product_id == product_id
        )
    )
    if row is not None:
        await db.delete(row)
        await db.commit()
