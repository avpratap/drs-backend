from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.scope import ALL, allowed_shop_ids
from app.models.shop import CustomerAccount, Shop
from app.models.user import User
from app.schemas.shop import ShopCreate, ShopOut

router = APIRouter(prefix="/shops", tags=["shops"])


@router.get("", response_model=list[ShopOut], operation_id="list_my_shops")
async def list_shops(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ids = await allowed_shop_ids(user, db)
    query = select(Shop)
    if ids is not ALL:
        if not ids:
            return []
        query = query.where(Shop.id.in_(ids))
    res = await db.execute(query.order_by(Shop.id))
    return res.scalars().all()


@router.post("", response_model=ShopOut, operation_id="create_shop")
async def create_shop(
    data: ShopCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role not in ("subscriber", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only subscribers can add shops")
    res = await db.execute(select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id))
    account = res.scalar_one_or_none()
    if account is None:
        account = CustomerAccount(
            owner_user_id=user.id, account_type="b2b", billing_mode="prepaid_wallet"
        )
        db.add(account)
        await db.flush()
    shop = Shop(account_id=account.id, name=data.name, address=data.address, status="active")
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


@router.get("/{shop_id}", response_model=ShopOut, operation_id="get_shop")
async def get_shop(
    shop_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ids = await allowed_shop_ids(user, db)
    # 404 (not 403) so we don't leak which shop ids exist
    if ids is not ALL and shop_id not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    shop = await db.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    return shop
