"""Coupon validation + application.

validate_and_reserve locks the coupon row, checks window / min-order / global +
per-user usage limits, computes the (capped) discount, and reserves one use by
incrementing used_count. The caller records a CouponRedemption once the order exists.
"""
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon, CouponRedemption
from app.services.subscription import today_ist


async def validate_and_reserve(
    db: AsyncSession, code: str, account_id: int, subtotal: float
) -> tuple[float, Coupon]:
    res = await db.execute(select(Coupon).where(Coupon.code == code).with_for_update())
    coupon = res.scalar_one_or_none()
    if coupon is None or not coupon.active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid coupon")

    today = today_ist()
    if coupon.valid_from and today < coupon.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Coupon not yet valid")
    if coupon.valid_to and today > coupon.valid_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Coupon expired")
    if subtotal < float(coupon.min_order_value):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Order below minimum ₹{float(coupon.min_order_value):.2f} for this coupon",
        )
    if coupon.usage_limit is not None and coupon.used_count >= coupon.usage_limit:
        raise HTTPException(status.HTTP_409_CONFLICT, "Coupon usage limit reached")
    if coupon.per_user_limit is not None:
        used_by_user = await db.scalar(
            select(func.count())
            .select_from(CouponRedemption)
            .where(
                CouponRedemption.coupon_id == coupon.id,
                CouponRedemption.account_id == account_id,
            )
        )
        if (used_by_user or 0) >= coupon.per_user_limit:
            raise HTTPException(status.HTTP_409_CONFLICT, "Coupon already used")

    if coupon.discount_type == "percent":
        discount = subtotal * float(coupon.value) / 100
        if coupon.max_discount is not None:
            discount = min(discount, float(coupon.max_discount))
    else:  # flat
        discount = float(coupon.value)
    discount = round(min(discount, subtotal), 2)  # never exceed the order

    coupon.used_count += 1  # reserve the use (row is locked)
    return discount, coupon


async def record_redemption(
    db: AsyncSession, coupon: Coupon, account_id: int, order_id: int, amount: float
) -> None:
    db.add(
        CouponRedemption(
            coupon_id=coupon.id,
            account_id=account_id,
            order_id=order_id,
            amount_discounted=amount,
        )
    )
