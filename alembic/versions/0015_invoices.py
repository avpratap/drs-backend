"""GST invoices + credit notes

Revision ID: 0015_invoices
Revises: 0014_refunds
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_invoices"
down_revision: str | None = "0014_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("invoice_no", sa.String(32), nullable=False),
        sa.Column("fy", sa.String(7), nullable=False),
        sa.Column("seq_no", sa.BigInteger(), nullable=False),
        sa.Column(
            "account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False
        ),
        sa.Column("delivery_order_id", sa.BigInteger(), sa.ForeignKey("delivery_orders.id")),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id")),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("seller_gstin", sa.String(15)),
        sa.Column("buyer_gstin", sa.String(15)),
        sa.Column("place_of_supply", sa.String(120)),
        sa.Column("is_interstate", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("taxable_value", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("cgst", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("sgst", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("igst", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("is_credit_note", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("original_invoice_id", sa.BigInteger(), sa.ForeignKey("invoices.id")),
        sa.Column("status", sa.String(12), server_default="issued", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("invoice_no", name="uq_invoice_no"),
        sa.UniqueConstraint("fy", "seq_no", name="uq_invoice_fy_seq"),
    )
    op.create_index("ix_invoices_account_id", "invoices", ["account_id"])
    # One tax invoice per delivery order (credit notes excluded).
    op.create_index(
        "uq_invoice_delivery_order",
        "invoices",
        ["delivery_order_id"],
        unique=True,
        postgresql_where=sa.text("delivery_order_id IS NOT NULL AND is_credit_note = false"),
    )

    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("invoice_id", sa.BigInteger(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id")),
        sa.Column("hsn_code", sa.String(16)),
        sa.Column("description", sa.String(255)),
        sa.Column("qty", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("taxable_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("gst_rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("cgst", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("sgst", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("igst", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("line_total", sa.Numeric(12, 2), nullable=False),
    )
    op.create_index("ix_invoice_lines_invoice_id", "invoice_lines", ["invoice_id"])


def downgrade() -> None:
    op.drop_table("invoice_lines")
    op.drop_index("uq_invoice_delivery_order", table_name="invoices")
    op.drop_index("ix_invoices_account_id", table_name="invoices")
    op.drop_table("invoices")
