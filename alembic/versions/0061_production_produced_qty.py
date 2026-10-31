"""production_orders.produced_qty: the actual finished-goods output of a run.

complete_order always booked planned_qty as output; it now accepts the actual yield and
records it here. Existing completed orders are backfilled with their planned_qty (what
was booked for them).

Revision ID: 0061_production_produced_qty
Revises: 0060_ledger_ref_unique
Create Date: 2026-10-31
"""
import sqlalchemy as sa

from alembic import op

revision = "0061_production_produced_qty"
down_revision = "0060_ledger_ref_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("production_orders", sa.Column("produced_qty", sa.Numeric(12, 3), nullable=True))
    op.execute("UPDATE production_orders SET produced_qty = planned_qty WHERE status = 'completed'")


def downgrade() -> None:
    op.drop_column("production_orders", "produced_qty")
