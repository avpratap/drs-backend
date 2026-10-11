"""Stock batches/lots with manufacture + expiry dates (FEFO picking for perishables)."""
import datetime as dt

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockBatch(Base):
    __tablename__ = "stock_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    mfg_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[dt.date | None] = mapped_column(Date, index=True, nullable=True)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)  # remaining
    status: Mapped[str] = mapped_column(
        String(12), default="active", nullable=False
    )  # active|depleted|expired
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
