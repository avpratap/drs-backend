"""DB backstop: at most one ledger row per real-world money event.

The application already makes these idempotent (bill_delivery checks _already_billed,
webhooks dedupe payments, ...); these partial UNIQUE indexes make a double-apply
impossible even if a code path regresses. Only (ref_type, txn/entry type) pairs that are
one-per-reference by definition are covered — refunds (reversal + release share a ref),
manual/admin entries (ref_id NULL) and the orphan-account path are untouched.

Fails loudly, naming the index, if existing data already holds duplicates (fix those
first; they are double-applied money).

Revision ID: 0060_ledger_ref_unique
Revises: 0059_applied_operations
Create Date: 2026-10-31
"""
import sqlalchemy as sa

from alembic import op

revision = "0060_ledger_ref_unique"
down_revision = "0059_applied_operations"
branch_labels = None
depends_on = None

# (index name, table, type column, type value, ref_type)
_INDEXES = [
    ("uq_wtxn_delivery_debit", "wallet_transactions", "txn_type", "debit_order", "delivery_order"),
    ("uq_wtxn_cash_collection", "wallet_transactions", "txn_type", "cash_collection", "cash_collection"),
    ("uq_wtxn_payment_topup", "wallet_transactions", "txn_type", "topup", "payment"),
    ("uq_wtxn_giftcard_redeem", "wallet_transactions", "txn_type", "giftcard", "gift_card_txn"),
    ("uq_wtxn_withdrawal", "wallet_transactions", "txn_type", "withdrawal", "withdrawal"),
    ("uq_credit_delivery_charge", "credit_ledger", "entry_type", "charge", "delivery_order"),
    ("uq_credit_cash_collection", "credit_ledger", "entry_type", "payment", "cash_collection"),
]


def upgrade() -> None:
    conn = op.get_bind()
    for name, table, col, value, ref_type in _INDEXES:
        dupes = conn.execute(
            sa.text(
                f"SELECT count(*) FROM (SELECT ref_id FROM {table} "
                f"WHERE {col} = :v AND ref_type = :r AND ref_id IS NOT NULL "
                "GROUP BY ref_id HAVING count(*) > 1) d"
            ),
            {"v": value, "r": ref_type},
        ).scalar()
        if dupes:
            raise RuntimeError(
                f"{name}: {dupes} {ref_type} reference(s) already have more than one "
                f"{table}.{col}='{value}' row (double-applied money); reconcile them before "
                "upgrading"
            )
        op.create_index(
            name, table, ["ref_id"], unique=True,
            postgresql_where=sa.text(f"{col} = '{value}' AND ref_type = '{ref_type}'"),
        )


def downgrade() -> None:
    for name, table, *_ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
