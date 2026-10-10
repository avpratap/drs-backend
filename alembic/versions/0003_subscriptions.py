"""subscriptions, subscription_items, subscription_overrides

Revision ID: 0003_subscriptions
Revises: 0002_auth_sessions
Create Date: 2026-10-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_subscriptions"
down_revision: Union[str, None] = "0002_auth_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("shop_id", sa.BigInteger(), sa.ForeignKey("shops.id"), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", name="uq_subscription_shop"),
    )
    op.create_index("ix_subscriptions_shop_id", "subscriptions", ["shop_id"])

    op.create_table(
        "subscription_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("subscription_id", sa.BigInteger(), sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("default_qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("frequency", sa.String(16), server_default="daily", nullable=False),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        sa.Column("days_of_week", sa.String(32), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscription_items_subscription_id", "subscription_items", ["subscription_id"])

    op.create_table(
        "subscription_overrides",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("subscription_item_id", sa.BigInteger(), sa.ForeignKey("subscription_items.id"), nullable=False),
        sa.Column("for_date", sa.Date(), nullable=False),
        sa.Column("override_qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("subscription_item_id", "for_date", name="uq_override_item_date"),
    )
    op.create_index(
        "ix_subscription_overrides_item_id", "subscription_overrides", ["subscription_item_id"]
    )


def downgrade() -> None:
    op.drop_table("subscription_overrides")
    op.drop_table("subscription_items")
    op.drop_table("subscriptions")
