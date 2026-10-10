"""payments

Revision ID: 0009_payments
Revises: 0008_orders
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_payments"
down_revision: str | None = "0008_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("payment_type", sa.String(20), nullable=False),
        sa.Column("gateway", sa.String(16), server_default="razorpay", nullable=False),
        sa.Column("gateway_order_id", sa.String(64), nullable=True),
        sa.Column("gateway_payment_id", sa.String(64), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(16), server_default="initiated", nullable=False),
        sa.Column("ref_type", sa.String(20), nullable=True),
        sa.Column("ref_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payments_account_id", "payments", ["account_id"])
    op.create_index("ix_payments_gateway_order_id", "payments", ["gateway_order_id"])


def downgrade() -> None:
    op.drop_table("payments")
