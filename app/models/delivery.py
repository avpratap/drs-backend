import datetime as dt

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ScheduleRun(Base):
    """Audit of one cutoff batch run."""

    __tablename__ = "delivery_schedule_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    for_delivery_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="generating", nullable=False)
    orders_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryOrder(Base, TimestampMixin):
    """One consolidated delivery per shop per day (subscription + one-time merged)."""

    __tablename__ = "delivery_orders"
    __table_args__ = (
        UniqueConstraint("shop_id", "delivery_date", name="uq_delivery_order_shop_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_date: Mapped[dt.date] = mapped_column(Date, index=True, nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("customer_accounts.id"), nullable=False)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), default="subscription", nullable=False)
    schedule_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_schedule_runs.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="generated", nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    amount_due: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)


class DeliveryOrderItem(Base):
    __tablename__ = "delivery_order_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_order_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty_planned: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    qty_delivered: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="planned", nullable=False)
