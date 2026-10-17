"""review management: moderation + verified-purchase + helpful votes + denorm aggregates

Revision ID: 0035_reviews
Revises: 0034_rma
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_reviews"
down_revision: str | None = "0034_rma"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ratings", sa.Column("status", sa.String(10), server_default="approved", nullable=False))
    op.add_column("ratings", sa.Column("verified_purchase", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("ratings", sa.Column("helpful_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("ratings", sa.Column("admin_reply", sa.String(1000)))
    op.add_column("ratings", sa.Column("replied_at", sa.DateTime(timezone=True)))

    op.add_column("products", sa.Column("avg_rating", sa.Numeric(3, 2), server_default="0", nullable=False))
    op.add_column("products", sa.Column("rating_count", sa.Integer(), server_default="0", nullable=False))

    op.create_table(
        "review_helpful",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("rating_id", sa.BigInteger(), sa.ForeignKey("ratings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("rating_id", "user_id", name="uq_review_helpful_user"),
    )
    op.create_index("ix_review_helpful_rating_id", "review_helpful", ["rating_id"])


def downgrade() -> None:
    op.drop_index("ix_review_helpful_rating_id", table_name="review_helpful")
    op.drop_table("review_helpful")
    op.drop_column("products", "rating_count")
    op.drop_column("products", "avg_rating")
    op.drop_column("ratings", "replied_at")
    op.drop_column("ratings", "admin_reply")
    op.drop_column("ratings", "helpful_count")
    op.drop_column("ratings", "verified_purchase")
    op.drop_column("ratings", "status")
