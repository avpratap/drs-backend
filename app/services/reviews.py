"""Review helpers: denormalized product aggregates + verified-purchase detection."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.rating import Rating
from app.models.shop import CustomerAccount


async def recompute_product_rating(db: AsyncSession, product_id: int) -> None:
    """Refresh Product.avg_rating + rating_count over APPROVED reviews only.
    Lock the product FIRST so concurrent recomputes serialize and each counts the other's
    committed rating (count-then-write without it dropped a vote)."""
    product = (
        await db.execute(select(Product).where(Product.id == product_id).with_for_update())
    ).scalar_one_or_none()
    count, avg = (
        await db.execute(
            select(func.count(), func.coalesce(func.avg(Rating.stars), 0)).where(
                Rating.product_id == product_id, Rating.status == "approved"
            )
        )
    ).one()
    if product is not None:
        product.rating_count = int(count)
        product.avg_rating = round(float(avg), 2)


async def is_verified_purchase(db: AsyncSession, user_id: int, product_id: int) -> bool:
    """True if the reviewer actually bought/received the product.

    Heuristic: a paid one-time Order OR a delivered standing-order line, in any of
    the user's own customer accounts, that includes this product.
    """
    acct_ids = select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user_id)
    paid = await db.scalar(
        select(func.count())
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.account_id.in_(acct_ids),
            Order.payment_status.in_(("paid", "partially_refunded")),
            OrderItem.product_id == product_id,
        )
    )
    if paid:
        return True
    delivered = await db.scalar(
        select(func.count())
        .select_from(DeliveryOrder)
        .join(DeliveryOrderItem, DeliveryOrderItem.delivery_order_id == DeliveryOrder.id)
        .where(
            DeliveryOrder.account_id.in_(acct_ids),
            DeliveryOrderItem.product_id == product_id,
            DeliveryOrderItem.status.in_(("delivered", "partial")),
        )
    )
    return bool(delivered)
