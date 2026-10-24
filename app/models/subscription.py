import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Subscription(Base, TimestampMixin):
    """A standing-order template for one shop (one per shop)."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("shop_id", name="uq_subscription_shop"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("customer_accounts.id"), nullable=False)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)  # active|paused|cancelled
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class SubscriptionItem(Base, TimestampMixin):
    """Per-product daily standing quantity + recurrence."""

    __tablename__ = "subscription_items"
    __table_args__ = (
        UniqueConstraint("subscription_id", "product_id", name="uq_subscription_items_sub_product"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    default_qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    frequency: Mapped[str] = mapped_column(String(16), default="daily", nullable=False)  # daily|alternate|weekly
    interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # for alternate
    days_of_week: Mapped[str | None] = mapped_column(String(32), nullable=True)  # csv "1,3,5" (Mon=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SubscriptionOverride(Base, TimestampMixin):
    """A one-day change to a standing item (skip / change qty) before the cutoff."""

    __tablename__ = "subscription_overrides"
    __table_args__ = (
        UniqueConstraint("subscription_item_id", "for_date", name="uq_override_item_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subscription_item_id: Mapped[int] = mapped_column(
        ForeignKey("subscription_items.id"), index=True, nullable=False
    )
    for_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    override_qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)  # 0 = skip
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
