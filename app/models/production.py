"""Production: bills of materials + production orders.

A BOM is a recipe: a finished product made from component products in fixed
quantities per `output_qty` units. A production order consumes the components
(FEFO) from a location and produces a finished-goods batch at that location —
turning raw milk/cream into ghee, paneer, etc., all through the movement ledger.
"""
import datetime as dt

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BillOfMaterials(Base):
    __tablename__ = "bills_of_materials"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    output_qty: Mapped[float] = mapped_column(Numeric(12, 3), default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)  # active|inactive
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BomComponent(Base):
    __tablename__ = "bom_components"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bom_id: Mapped[int] = mapped_column(
        ForeignKey("bills_of_materials.id", ondelete="CASCADE"), index=True, nullable=False
    )
    component_product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)  # per output_qty units


class ProductionOrder(Base):
    __tablename__ = "production_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pro_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    bom_id: Mapped[int] = mapped_column(ForeignKey("bills_of_materials.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    planned_qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="draft", nullable=False
    )  # draft|in_progress|completed|cancelled
    output_batch_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Actual finished-goods output booked on completion (None until completed).
    produced_qty: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    mfg_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
