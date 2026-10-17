"""conveniences: wishlist + newsletter subscribers

Revision ID: 0043_conveniences
Revises: 0042_delivery_ops
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043_conveniences"
down_revision: str | None = "0042_delivery_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wishlist_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id", "product_id", name="uq_wishlist_account_product"),
    )
    op.create_index("ix_wishlist_account_id", "wishlist_items", ["account_id"])

    op.create_table(
        "newsletter_subscribers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("name", sa.String(120)),
        sa.Column("status", sa.String(16), server_default="subscribed", nullable=False),
        sa.Column("source", sa.String(32)),
        sa.Column("consent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("email", name="uq_newsletter_email"),
    )


def downgrade() -> None:
    op.drop_table("newsletter_subscribers")
    op.drop_index("ix_wishlist_account_id", table_name="wishlist_items")
    op.drop_table("wishlist_items")
