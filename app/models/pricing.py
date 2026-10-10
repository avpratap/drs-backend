import datetime as dt

from sqlalchemy import BigInteger, Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PriceTier(Base):
    __tablename__ = "price_tiers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # RETAIL|B2B_STANDARD|HORECA
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    tier_type: Mapped[str] = mapped_column(String(16), default="retail", nullable=False)  # retail|b2b


class ProductPrice(Base, TimestampMixin):
    __tablename__ = "product_prices"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "price_tier_id", "min_qty", "valid_from", name="uq_product_price"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    price_tier_id: Mapped[int] = mapped_column(ForeignKey("price_tiers.id"), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    min_qty: Mapped[float] = mapped_column(Numeric(10, 2), default=1, nullable=False)  # volume break
    valid_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
