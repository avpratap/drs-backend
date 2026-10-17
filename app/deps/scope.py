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
    # Subscribers (B2B) and retail customers both own their shops via their account
    # (a retail customer's "home shop" is their delivery address). They see only their
    # own account's shops — no cross-account access.
    if user.role in ("subscriber", "customer"):
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
