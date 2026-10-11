"""ratings + wallet withdrawal requests

Revision ID: 0022_ratings_wd
Revises: 0021_coupons
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_ratings_wd"
down_revision: str | None = "0021_coupons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ratings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "product_id", name="uq_rating_user_product"),
        sa.CheckConstraint("stars BETWEEN 1 AND 5", name="ck_rating_stars"),
    )
    op.create_index("ix_ratings_product_id", "ratings", ["product_id"])

    op.create_table(
        "withdrawal_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(12), server_default="requested", nullable=False),
        sa.Column("note", sa.String(255)),
        sa.Column("decided_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount > 0", name="ck_withdrawal_amount_pos"),
    )
    op.create_index("ix_withdrawal_requests_account_id", "withdrawal_requests", ["account_id"])
    op.create_index("ix_withdrawal_requests_status", "withdrawal_requests", ["status"])


def downgrade() -> None:
    op.drop_table("withdrawal_requests")
    op.drop_index("ix_ratings_product_id", table_name="ratings")
    op.drop_table("ratings")
