"""invoices.is_partial — mark partial (RMA per-line) credit notes so a later full
reversal's idempotency check doesn't match-and-skip against a partial one.

Revision ID: 0050_inv_partial
Revises: 0049_notif_toaddr
Create Date: 2026-10-17
"""
import sqlalchemy as sa

from alembic import op

revision = "0050_inv_partial"
down_revision = "0049_notif_toaddr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("is_partial", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("invoices", "is_partial")
