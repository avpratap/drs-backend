"""coupons + redemptions + order discount

Revision ID: 0021_coupons
Revises: 0020_inventory
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_coupons"
down_revision: str | None = "0020_inventory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("discount", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("orders", sa.Column("coupon_code", sa.String(32)))

    op.create_table(
        "coupons",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("discount_type", sa.String(8), nullable=False),
        sa.Column("value", sa.Numeric(12, 2), nullable=False),
        sa.Column("min_order_value", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("max_discount", sa.Numeric(12, 2)),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_to", sa.Date()),
        sa.Column("usage_limit", sa.Integer()),
        sa.Column("used_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("per_user_limit", sa.Integer()),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("code", name="uq_coupon_code"),
        sa.CheckConstraint("value > 0", name="ck_coupon_value_pos"),
    )
    op.create_table(
        "coupon_redemptions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("coupon_id", sa.BigInteger(), sa.ForeignKey("coupons.id"), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id")),
        sa.Column("amount_discounted", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_coupon_redemptions_coupon_id", "coupon_redemptions", ["coupon_id"])
    op.create_index("ix_coupon_redemptions_account_id", "coupon_redemptions", ["account_id"])


def downgrade() -> None:
    op.drop_table("coupon_redemptions")
    op.drop_table("coupons")
    op.drop_column("orders", "coupon_code")
    op.drop_column("orders", "discount")
