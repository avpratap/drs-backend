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
                Payment.is_excess.is_(False),  # a duplicate capture's refund isn't the order's
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


async def refunded_settled(db: AsyncSession, order_id: int) -> float:
    """Money DEFINITELY handed back on the order (processed refunds to source on its own
    payments + RMA store credit). Credit notes and "fully refunded" are based on this —
    a pending/initiated refund may still fail. (The cap uses the non-failed sum.)"""
    to_source = float(
        await db.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0))
            .join(Payment, Payment.id == Refund.payment_id)
            .where(
                Payment.ref_type == "order",
                Payment.ref_id == order_id,
                Payment.is_excess.is_(False),
                Refund.status == "processed",
            )
        )
        or 0
    )
    return round(to_source + await refunded_via_rma(db, order_id), 2)


async def credited_refunds(db: AsyncSession, order_id: int) -> float:
    """Settled refunds whose credit-note effect has been APPLIED (a processed refund
    still waiting for its ledger effect will add its own credit-note lines)."""
    to_source = float(
        await db.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0))
            .join(Payment, Payment.id == Refund.payment_id)
            .where(
                Payment.ref_type == "order",
                Payment.ref_id == order_id,
                Payment.is_excess.is_(False),
                Refund.status == "processed",
                Refund.ledger_applied_at.is_not(None),
            )
        )
        or 0
    )
    return round(to_source + await refunded_via_rma(db, order_id), 2)


async def order_refund_headroom(db: AsyncSession, order: Order) -> float:
    """How much more can be refunded on this order across BOTH paths."""
    used = await refunded_to_source(db, order.id) + await refunded_via_rma(db, order.id)
    return max(round(float(order.total) - used, 2), 0.0)


# Orders whose payment was captured (whatever has since been refunded).
SETTLED_STATUSES = ("paid", "partially_refunded", "refunded")


def net_order_value():
    """SQL expression: an order's total minus everything refunded on it (processed refunds
    to source on its own payments + RMA store credit) — the revenue it actually kept."""
    to_source = (
        select(func.coalesce(func.sum(Refund.amount), 0))
        .join(Payment, Payment.id == Refund.payment_id)
        .where(
            Payment.ref_type == "order",
            Payment.ref_id == Order.id,
            Payment.is_excess.is_(False),
            Refund.status == "processed",
        )
        .correlate(Order)
        .scalar_subquery()
    )
    via_rma = (
        select(func.coalesce(func.sum(ReturnRequest.refund_amount), 0))
        .where(ReturnRequest.order_id == Order.id, ReturnRequest.status == "refunded")
        .correlate(Order)
        .scalar_subquery()
    )
    return Order.total - to_source - via_rma
