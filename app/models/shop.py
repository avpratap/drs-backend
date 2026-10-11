from geoalchemy2 import Geography
from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CustomerAccount(Base, TimestampMixin):
    """Billing entity. A B2B subscriber owns one account that owns many shops."""

    __tablename__ = "customer_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_type: Mapped[str] = mapped_column(String(16), default="retail", nullable=False)  # retail|b2b
    billing_mode: Mapped[str] = mapped_column(
        String(20), default="prepaid_wallet", nullable=False
    )  # prepaid_wallet|postpaid_credit
    price_tier_id: Mapped[int | None] = mapped_column(ForeignKey("price_tiers.id"), nullable=True)


class Shop(Base, TimestampMixin):
    """A supply location. One account -> many shops (the multi-shop model)."""

    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)  # GST place of supply
    pincode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    geo: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True
    )
    price_tier_id: Mapped[int | None] = mapped_column(ForeignKey("price_tiers.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)


class DeliveryAssignment(Base, TimestampMixin):
    """One delivery user -> many shops (many-to-many)."""

    __tablename__ = "delivery_assignments"
    __table_args__ = (UniqueConstraint("delivery_id", "shop_id", name="uq_delivery_shop"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
