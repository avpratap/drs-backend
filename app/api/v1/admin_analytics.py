"""Admin analytics: dashboard KPIs, revenue time-series, top products (read-only)."""
import datetime as dt

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps.auth import require_role
from app.models.billing import CreditLedger, Wallet
from app.models.delivery import DeliveryOrder
from app.models.inventory import StockItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.subscription import Subscription
from app.services import refunds as refund_acct

router = APIRouter(
    prefix="/admin/analytics", tags=["admin-analytics"],
    dependencies=[Depends(require_role("admin"))],
)


class OverviewOut(BaseModel):
    paid_order_revenue: float
    orders: int
    active_subscriptions: int
    delivered_orders: int
    low_stock_items: int
    credit_outstanding: float
    wallet_float: float


class RevenuePoint(BaseModel):
    date: str
    revenue: float
    orders: int


class TopProductOut(BaseModel):
    product_id: int
    name: str | None = None
    qty: float
    revenue: float


@router.get("/overview", response_model=OverviewOut, operation_id="analytics_overview")
async def overview(db: AsyncSession = Depends(get_db)):
    paid_revenue = await db.scalar(
        select(func.coalesce(func.sum(refund_acct.net_order_value()), 0)).where(
            Order.payment_status.in_(refund_acct.SETTLED_STATUSES)
        )
    )
    orders = await db.scalar(select(func.count()).select_from(Order))
    active_subs = await db.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.status == "active")
    )
    delivered = await db.scalar(
        select(func.count()).select_from(DeliveryOrder).where(DeliveryOrder.status == "delivered")
    )
    low_stock = await db.scalar(
        select(func.count())
        .select_from(StockItem)
        .where(StockItem.reorder_point > 0, StockItem.qty_on_hand <= StockItem.reorder_point)
    )
    wallet_float = await db.scalar(select(func.coalesce(func.sum(Wallet.balance), 0)))
    # Outstanding postpaid credit = latest balance per account, summed.
    latest = (
        select(CreditLedger.account_id, func.max(CreditLedger.id).label("mx"))
        .group_by(CreditLedger.account_id)
        .subquery()
    )
    outstanding = await db.scalar(
        select(func.coalesce(func.sum(func.greatest(CreditLedger.balance_after, 0)), 0)).join(
            latest, CreditLedger.id == latest.c.mx
        )
    )
    return OverviewOut(
        paid_order_revenue=float(paid_revenue or 0),
        orders=orders or 0,
        active_subscriptions=active_subs or 0,
        delivered_orders=delivered or 0,
        low_stock_items=low_stock or 0,
        credit_outstanding=float(outstanding or 0),
        wallet_float=float(wallet_float or 0),
    )


@router.get("/revenue", response_model=list[RevenuePoint], operation_id="analytics_revenue")
async def revenue(
    days: int = Query(default=30, ge=1, le=365), db: AsyncSession = Depends(get_db)
):
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    # Bucket by IST calendar day, not the DB's UTC — else early-morning IST orders fall
    # into the previous day's revenue.
    day = func.date(func.timezone(settings.TIMEZONE, Order.created_at))
    rows = (
        await db.execute(
            select(
                day.label("d"),
                func.coalesce(func.sum(refund_acct.net_order_value()), 0).label("rev"),
                func.count().label("n"),
            )
            .where(
                Order.payment_status.in_(refund_acct.SETTLED_STATUSES), Order.created_at >= since
            )
            .group_by(day)
            .order_by(day)
        )
    ).all()
    return [RevenuePoint(date=str(r.d), revenue=float(r.rev or 0), orders=int(r.n)) for r in rows]


@router.get("/top-products", response_model=list[TopProductOut], operation_id="analytics_top_products")
async def top_products(
    limit: int = Query(default=10, ge=1, le=100),
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    rev = func.coalesce(func.sum(OrderItem.line_total), 0)
    rows = (
        await db.execute(
            select(
                OrderItem.product_id,
                Product.name,
                func.coalesce(func.sum(OrderItem.qty), 0).label("qty"),
                rev.label("rev"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(
                Order.payment_status.in_(("paid", "partially_refunded")), Order.created_at >= since
            )
            .group_by(OrderItem.product_id, Product.name)
            .order_by(rev.desc())
            .limit(limit)
        )
    ).all()
    return [
        TopProductOut(product_id=r.product_id, name=r.name, qty=float(r.qty or 0), revenue=float(r.rev or 0))
        for r in rows
    ]
