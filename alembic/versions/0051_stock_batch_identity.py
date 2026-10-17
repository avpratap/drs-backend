"""Unique batch identity: one ACTIVE lot per (product, location, batch_no, expiry).

Backs the merge-on-receive in batches.receive_batch (#26): without this, two concurrent
first-receives of the same lot both find no row (FOR UPDATE on zero rows takes no gap
lock) and both INSERT -> duplicate active lots. The partial unique index gives the second
insert something to collide on, so the begin_nested/IntegrityError path merges instead.

NULLS NOT DISTINCT (PG15+) so two active lots with a NULL expiry_date still collide
(a plain unique index treats NULLs as distinct and would let NULL-expiry dups through).

Revision ID: 0051_batch_identity
Revises: 0050_inv_partial
Create Date: 2026-10-18
"""
from alembic import op

revision = "0051_batch_identity"
down_revision = "0050_inv_partial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_stock_batch_active_identity "
        "ON stock_batches (product_id, location_id, batch_no, expiry_date) "
        "NULLS NOT DISTINCT WHERE status = 'active'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_stock_batch_active_identity")
