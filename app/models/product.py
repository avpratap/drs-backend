from sqlalchemy import BigInteger, Boolean, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    uom: Mapped[str] = mapped_column(String(16), default="piece", nullable=False)  # litre|kg|piece|pack
    pack_size: Mapped[float] = mapped_column(Numeric(10, 2), default=1, nullable=False)
    is_subscribable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    perishable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hsn_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gst_rate: Mapped[float] = mapped_column(Numeric(4, 2), default=0, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
