"""price_tiers, product_prices + price_tier_id on accounts/shops + totals on delivery orders

Revision ID: 0006_pricing
Revises: 0005_fulfillment
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_pricing"
down_revision: str | None = "0005_fulfillment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_tiers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("tier_type", sa.String(16), server_default="retail", nullable=False),
        sa.UniqueConstraint("code", name="uq_price_tiers_code"),
    )

    op.create_table(
        "product_prices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("price_tier_id", sa.BigInteger(), sa.ForeignKey("price_tiers.id"), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("min_qty", sa.Numeric(10, 2), server_default="1", nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("product_id", "price_tier_id", "min_qty", "valid_from", name="uq_product_price"),
    )
    op.create_index("ix_product_prices_product_id", "product_prices", ["product_id"])

    op.add_column(
        "customer_accounts",
        sa.Column("price_tier_id", sa.BigInteger(), sa.ForeignKey("price_tiers.id"), nullable=True),
    )
    op.add_column(
        "shops",
        sa.Column("price_tier_id", sa.BigInteger(), sa.ForeignKey("price_tiers.id"), nullable=True),
    )

    op.add_column("delivery_orders", sa.Column("subtotal", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("delivery_orders", sa.Column("total", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("delivery_orders", sa.Column("amount_due", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("delivery_order_items", sa.Column("unit_price", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("delivery_order_items", sa.Column("line_total", sa.Numeric(12, 2), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("delivery_order_items", "line_total")
    op.drop_column("delivery_order_items", "unit_price")
    op.drop_column("delivery_orders", "amount_due")
    op.drop_column("delivery_orders", "total")
    op.drop_column("delivery_orders", "subtotal")
    op.drop_column("shops", "price_tier_id")
    op.drop_column("customer_accounts", "price_tier_id")
    op.drop_table("product_prices")
    op.drop_table("price_tiers")
