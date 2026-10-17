"""Stock levels + an append-only stock-movement ledger (receipts, adjustments,
wastage). Current on-hand is a locked aggregate maintained alongside the ledger."""
import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockItem(Base):
    __tablename__ = "stock_items"
    __table_args__ = (
        UniqueConstraint("product_id", "location_id", name="uq_stock_product_location"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    qty_on_hand: Mapped[float] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    reorder_point: Mapped[float] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    # FK declared to match the DB constraint fk_stock_movements_batch (migration 0025).
    batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("stock_batches.id"), nullable=True
    )
    change: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)  # signed
    reason: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # receipt|adjustment|wastage|return|delivery|transfer|production|consume
    ref_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
