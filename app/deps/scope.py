"""Shop-level authorization scoping (the Supabase-RLS replacement).

allowed_shop_ids returns the set of shop ids the user may touch, or the ALL
sentinel for admins. Every shop-scoped query MUST be filtered through this.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
from app.models.user import User

ALL = object()  # sentinel meaning "no restriction" (admins)


async def allowed_shop_ids(user: User, db: AsyncSession):
    if user.role == "admin":
        return ALL
    if user.role == "subscriber":
        res = await db.execute(
            select(Shop.id)
            .join(CustomerAccount, Shop.account_id == CustomerAccount.id)
            .where(CustomerAccount.owner_user_id == user.id)
        )
        return set(res.scalars().all())
    if user.role == "delivery":
        res = await db.execute(
            select(DeliveryAssignment.shop_id).where(
                DeliveryAssignment.delivery_id == user.id,
                DeliveryAssignment.active.is_(True),
            )
        )
        return set(res.scalars().all())
    return set()  # customer: no shop scope
