"""churn scores (ML risk per account)

Revision ID: 0044_churn
Revises: 0043_conveniences
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0044_churn"
down_revision: str | None = "0043_conveniences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "churn_scores",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("score", sa.Numeric(4, 3), nullable=False),
        sa.Column("risk", sa.String(8), nullable=False),
        sa.Column("features", postgresql.JSONB()),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id", name="uq_churn_account"),
    )
    op.create_index("ix_churn_scores_account_id", "churn_scores", ["account_id"])
    op.create_index("ix_churn_scores_risk", "churn_scores", ["risk"])


def downgrade() -> None:
    op.drop_index("ix_churn_scores_risk", table_name="churn_scores")
    op.drop_index("ix_churn_scores_account_id", table_name="churn_scores")
    op.drop_table("churn_scores")
