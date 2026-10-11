"""refunds + webhook events

Revision ID: 0014_refunds
Revises: 0013_idempotency
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_refunds"
down_revision: str | None = "0013_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refunds",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("payment_id", sa.BigInteger(), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column(
            "account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.String(255)),
        sa.Column("status", sa.String(12), server_default="initiated", nullable=False),
        sa.Column("gateway_refund_id", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_refunds_payment_id", "refunds", ["payment_id"])
    op.create_index("ix_refunds_account_id", "refunds", ["account_id"])

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64)),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("event_id", name="uq_webhook_event_id"),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_index("ix_refunds_account_id", table_name="refunds")
    op.drop_index("ix_refunds_payment_id", table_name="refunds")
    op.drop_table("refunds")
