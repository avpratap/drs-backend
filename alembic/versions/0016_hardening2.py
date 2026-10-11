"""hardening 2: mandate Numeric, credit-note unique, fk index, money CHECKs

Revision ID: 0016_hardening2
Revises: 0015_invoices
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_hardening2"
down_revision: str | None = "0015_invoices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Money cap stored as Numeric, not float (RBI UPI Autopay per-debit cap).
    op.alter_column(
        "payment_mandates",
        "max_amount",
        type_=sa.Numeric(12, 2),
        existing_nullable=False,
        postgresql_using="max_amount::numeric(12,2)",
    )

    # At most one credit note per source invoice (concurrency backstop).
    op.create_index(
        "uq_credit_note_original",
        "invoices",
        ["original_invoice_id"],
        unique=True,
        postgresql_where=sa.text("is_credit_note = true AND original_invoice_id IS NOT NULL"),
    )

    # Hot FK join for per-account billing / invoicing.
    op.create_index("ix_delivery_orders_account_id", "delivery_orders", ["account_id"])

    # Non-negative money guards where the sign is fixed.
    op.create_check_constraint("ck_wallets_balance_nonneg_floor", "wallets", "balance >= -1000000")
    op.create_check_constraint("ck_payments_amount_pos", "payments", "amount > 0")
    op.create_check_constraint("ck_refunds_amount_pos", "refunds", "amount > 0")
    op.create_check_constraint("ck_mandate_amount_pos", "payment_mandates", "max_amount > 0")
    op.create_check_constraint("ck_invoice_total_nonneg", "invoices", "total >= 0 OR is_credit_note")


def downgrade() -> None:
    op.drop_constraint("ck_invoice_total_nonneg", "invoices", type_="check")
    op.drop_constraint("ck_mandate_amount_pos", "payment_mandates", type_="check")
    op.drop_constraint("ck_refunds_amount_pos", "refunds", type_="check")
    op.drop_constraint("ck_payments_amount_pos", "payments", type_="check")
    op.drop_constraint("ck_wallets_balance_nonneg_floor", "wallets", type_="check")
    op.drop_index("ix_delivery_orders_account_id", table_name="delivery_orders")
    op.drop_index("uq_credit_note_original", table_name="invoices")
    op.alter_column(
        "payment_mandates",
        "max_amount",
        type_=sa.Float(),
        existing_nullable=False,
        postgresql_using="max_amount::double precision",
    )
