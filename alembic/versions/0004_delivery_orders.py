"""delivery_schedule_runs, delivery_orders, delivery_order_items

Revision ID: 0004_delivery_orders
Revises: 0003_subscriptions
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_delivery_orders"
down_revision: str | None = "0003_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_schedule_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("for_delivery_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), server_default="generating", nullable=False),
        sa.Column("orders_generated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "delivery_orders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("delivery_date", sa.Date(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), sa.ForeignKey("shops.id"), nullable=False),
        sa.Column("origin", sa.String(16), server_default="subscription", nullable=False),
        sa.Column("schedule_run_id", sa.BigInteger(), sa.ForeignKey("delivery_schedule_runs.id"), nullable=True),
        sa.Column("status", sa.String(20), server_default="generated", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "delivery_date", name="uq_delivery_order_shop_date"),
    )
    op.create_index("ix_delivery_orders_delivery_date", "delivery_orders", ["delivery_date"])
    op.create_index("ix_delivery_orders_shop_id", "delivery_orders", ["shop_id"])

    op.create_table(
        "delivery_order_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "delivery_order_id",
            sa.BigInteger(),
            sa.ForeignKey("delivery_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty_planned", sa.Numeric(10, 2), nullable=False),
        sa.Column("qty_delivered", sa.Numeric(10, 2), server_default="0", nullable=False),
        sa.Column("status", sa.String(16), server_default="planned", nullable=False),
    )
    op.create_index(
        "ix_delivery_order_items_order_id", "delivery_order_items", ["delivery_order_id"]
    )


def downgrade() -> None:
    op.drop_table("delivery_order_items")
    op.drop_table("delivery_orders")
    op.drop_table("delivery_schedule_runs")
