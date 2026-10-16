"""Stock transfers between locations.

Two-phase so hub/vehicle movement is modelled honestly: dispatch decrements the
source (FEFO) and snapshots the exact lots that left; receive re-creates those
lots at the destination, preserving batch_no / mfg / expiry (lot identity travels
with the goods). Everything flows through the same movement ledger.
"""
import datetime as dt

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockTransfer(Base):
    __tablename__ = "stock_transfers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transfer_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="draft", nullable=False
    )  # draft|in_transit|received|cancelled
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockTransferItem(Base):
    __tablename__ = "stock_transfer_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transfer_id: Mapped[int] = mapped_column(
        ForeignKey("stock_transfers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)


class StockTransferLot(Base):
    """Snapshot of the lots consumed at dispatch — replayed to rebuild batches on receive."""

    __tablename__ = "stock_transfer_lots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transfer_item_id: Mapped[int] = mapped_column(
        ForeignKey("stock_transfer_items.id", ondelete="CASCADE"), index=True, nullable=False
    )
    batch_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mfg_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
