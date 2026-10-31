"""applied_operations: handler-level idempotency record for money endpoints.

A row is written in the same transaction as an admin wallet topup / adjust or credit
settlement, keyed by (scope, "<actor id>:<Idempotency-Key>"), so a retry that slips past
the middleware (its claim reaped after a crash between the money commit and the response
store) replays the stored result instead of moving money twice.

Revision ID: 0059_applied_operations
Revises: 0058_backfill_retail_min_qty
Create Date: 2026-10-31
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0059_applied_operations"
down_revision = "0058_backfill_retail_min_qty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "applied_operations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("scope", "key", name="uq_applied_operation"),
    )


def downgrade() -> None:
    op.drop_table("applied_operations")
