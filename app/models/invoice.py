"""GST tax invoices + credit notes.

One invoice per delivered delivery_order (idempotent). GST is split CGST+SGST for
intra-state supply or IGST for inter-state, decided by the buyer's place of supply
vs the seller's state. A credit note is a negative invoice referencing the original
(used for returns / refunds).
"""
import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("invoice_no", name="uq_invoice_no"),
        UniqueConstraint("fy", "seq_no", name="uq_invoice_fy_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(32), nullable=False)
    fy: Mapped[str] = mapped_column(String(7), nullable=False)  # e.g. 2026-27
    seq_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    delivery_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_orders.id"), nullable=True
    )
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    invoice_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    seller_gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    buyer_gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    place_of_supply: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_interstate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    taxable_value: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    cgst: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    sgst: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    igst: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    is_credit_note: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    original_invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(12), default="issued", nullable=False)
    pdf_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id"), index=True, nullable=False
    )
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    hsn_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    taxable_value: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    gst_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    cgst: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    sgst: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    igst: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
