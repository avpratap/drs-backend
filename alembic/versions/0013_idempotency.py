"""idempotency keys

Revision ID: 0013_idempotency
Revises: 0012_notifications
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_idempotency"
down_revision: str | None = "0012_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_body", sa.Text()),
        sa.Column("response_media_type", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("key", name="uq_idempotency_key"),
    )
    op.create_index("ix_idempotency_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_created_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
