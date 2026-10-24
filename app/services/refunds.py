"""Cross-path refund accounting for one-time orders.

An order can be refunded two ways: an RMA refund (store credit to the wallet) and a
refund to source against its payment. Both paths must respect ONE cap — what the
customer actually paid — or the order can be refunded twice. Callers hold the Order
row lock (FOR UPDATE) so the two paths serialize on it.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.payment import Payment
from app.models.refund import Refund
from app.models.rma import ReturnRequest


async def lock_order(db: AsyncSession, order_id: int) -> Order | None:
    return (
        await db.execute(
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def refunded_to_source(db: AsyncSession, order_id: int) -> float:
    """Gateway refunds that moved (or may have moved) money: every non-failed refund."""
    return float(
        await db.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0))
            .join(Payment, Payment.id == Refund.payment_id)
            .where(
                Payment.ref_type == "order",
                Payment.ref_id == order_id,
                Refund.status != "failed",
            )
        )
        or 0
    )


async def refunded_via_rma(db: AsyncSession, order_id: int) -> float:
    return float(
        await db.scalar(
            select(func.coalesce(func.sum(ReturnRequest.refund_amount), 0)).where(
                ReturnRequest.order_id == order_id, ReturnRequest.status == "refunded"
            )
        )
        or 0
    )


async def order_refund_headroom(db: AsyncSession, order: Order) -> float:
    """How much more can be refunded on this order across BOTH paths."""
    used = await refunded_to_source(db, order.id) + await refunded_via_rma(db, order.id)
    return max(round(float(order.total) - used, 2), 0.0)
