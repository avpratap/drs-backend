"""procurement: suppliers + purchase orders + items

Revision ID: 0026_procurement
Revises: 0025_batches
Create Date: 2026-10-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_procurement"
down_revision: str | None = "0025_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("gstin", sa.String(15)),
        sa.Column("contact_phone", sa.String(16)),
        sa.Column("contact_email", sa.String(255)),
        sa.Column("address", sa.String(512)),
        sa.Column("state", sa.String(120)),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("po_no", sa.String(32), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("location_id", sa.BigInteger(), sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("expected_date", sa.Date()),
        sa.Column("subtotal", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("po_no", name="uq_po_no"),
    )
    op.create_index("ix_purchase_orders_supplier_id", "purchase_orders", ["supplier_id"])
    op.create_table(
        "purchase_order_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("purchase_order_id", sa.BigInteger(), sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty_ordered", sa.Numeric(12, 3), nullable=False),
        sa.Column("qty_received", sa.Numeric(12, 3), server_default="0", nullable=False),
        sa.Column("unit_cost", sa.Numeric(12, 2), nullable=False),
        sa.Column("line_total", sa.Numeric(12, 2), nullable=False),
    )
    op.create_index("ix_po_items_po_id", "purchase_order_items", ["purchase_order_id"])


def downgrade() -> None:
    op.drop_table("purchase_order_items")
    op.drop_index("ix_purchase_orders_supplier_id", table_name="purchase_orders")
    op.drop_table("purchase_orders")
    op.drop_table("suppliers")
