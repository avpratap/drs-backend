"""orders, order_items (one-time e-commerce)

Revision ID: 0008_orders
Revises: 0007_billing
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_orders"
down_revision: str | None = "0007_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("order_no", sa.String(32), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), sa.ForeignKey("shops.id"), nullable=False),
        sa.Column("order_type", sa.String(20), server_default="one_time", nullable=False),
        sa.Column("status", sa.String(20), server_default="created", nullable=False),
        sa.Column("subtotal", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("payment_status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_no", name="uq_orders_order_no"),
    )
    op.create_index("ix_orders_shop_id", "orders", ["shop_id"])

    op.create_table(
        "order_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("line_total", sa.Numeric(12, 2), server_default="0", nullable=False),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])


def downgrade() -> None:
    op.drop_table("order_items")
    op.drop_table("orders")
