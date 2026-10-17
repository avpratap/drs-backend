"""loyalty points + referral program + gift cards

Revision ID: 0039_loyalty
Revises: 0038_cms
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_loyalty"
down_revision: str | None = "0038_cms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Append-only ledgers (drs_append_only() is defined in migration 0018).
_APPEND_ONLY = ("points_ledger", "gift_card_transactions")


def upgrade() -> None:
    op.create_table(
        "loyalty_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("points_balance", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id", name="uq_loyalty_account"),
    )
    op.create_table(
        "points_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("ref_type", sa.String(24)),
        sa.Column("ref_id", sa.BigInteger()),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_points_ledger_account_id", "points_ledger", ["account_id"])

    op.create_table(
        "referral_codes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("reward_points", sa.Integer(), server_default="100", nullable=False),
        sa.Column("referee_discount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("status", sa.String(10), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id", name="uq_referral_account"),
        sa.UniqueConstraint("code", name="uq_referral_code"),
    )
    op.create_table(
        "referral_redemptions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code_id", sa.BigInteger(), sa.ForeignKey("referral_codes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("referee_account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("code_id", "referee_account_id", name="uq_referral_once_per_referee"),
    )
    op.create_index("ix_referral_redemptions_code_id", "referral_redemptions", ["code_id"])

    op.create_table(
        "gift_cards",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("initial_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(10), server_default="active", nullable=False),
        sa.Column("issued_to_account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id")),
        sa.Column("expires_at", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("code", name="uq_gift_card_code"),
    )
    op.create_index("ix_gift_cards_issued_to", "gift_cards", ["issued_to_account_id"])
    op.create_table(
        "gift_card_transactions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("gift_card_id", sa.BigInteger(), sa.ForeignKey("gift_cards.id"), nullable=False),
        sa.Column("delta", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.String(10), nullable=False),
        sa.Column("ref_type", sa.String(24)),
        sa.Column("ref_id", sa.BigInteger()),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_gift_card_txns_card_id", "gift_card_transactions", ["gift_card_id"])

    for t in _APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER trg_{t}_append_only "
            f"BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION drs_append_only();"
        )


def downgrade() -> None:
    for t in _APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_append_only ON {t};")
    op.drop_index("ix_gift_card_txns_card_id", table_name="gift_card_transactions")
    op.drop_table("gift_card_transactions")
    op.drop_index("ix_gift_cards_issued_to", table_name="gift_cards")
    op.drop_table("gift_cards")
    op.drop_index("ix_referral_redemptions_code_id", table_name="referral_redemptions")
    op.drop_table("referral_redemptions")
    op.drop_table("referral_codes")
    op.drop_index("ix_points_ledger_account_id", table_name="points_ledger")
    op.drop_table("points_ledger")
    op.drop_table("loyalty_accounts")
