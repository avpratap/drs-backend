"""wallets, wallet_transactions, credit_ledger

Revision ID: 0007_billing
Revises: 0006_pricing
Create Date: 2026-10-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_billing"
down_revision: Union[str, None] = "0006_pricing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("balance", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id", name="uq_wallets_account"),
    )

    op.create_table(
        "wallet_transactions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("wallet_id", sa.BigInteger(), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("txn_type", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("ref_type", sa.String(20), nullable=True),
        sa.Column("ref_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wallet_txns_wallet_id", "wallet_transactions", ["wallet_id"])

    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("ref_type", sa.String(20), nullable=True),
        sa.Column("ref_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_credit_ledger_account_id", "credit_ledger", ["account_id"])


def downgrade() -> None:
    op.drop_table("credit_ledger")
    op.drop_table("wallet_transactions")
    op.drop_table("wallets")
