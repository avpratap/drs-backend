from sqlalchemy import BigInteger, Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    uom: Mapped[str] = mapped_column(String(16), default="piece", nullable=False)  # litre|kg|piece|pack
    pack_size: Mapped[float] = mapped_column(Numeric(10, 3), default=1, nullable=False)
    is_subscribable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    perishable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hsn_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gst_rate: Mapped[float] = mapped_column(Numeric(4, 2), default=0, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # Courier shipping metadata (one-time e-commerce orders).
    weight_kg: Mapped[float | None] = mapped_column(Numeric(8, 3), nullable=True)
    length_cm: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    width_cm: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    height_cm: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    # Denormalized review aggregates (recomputed over approved reviews on each change).
    avg_rating: Mapped[float] = mapped_column(Numeric(3, 2), default=0, nullable=False)
    rating_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
