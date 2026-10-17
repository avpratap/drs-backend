"""notifications completion: device tokens + retry-backoff columns

Revision ID: 0040_notifications2
Revises: 0039_loyalty
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_notifications2"
down_revision: str | None = "0039_loyalty"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token", sa.String(512), nullable=False),
        sa.Column("platform", sa.String(10), server_default="android", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("token", name="uq_device_token"),
    )
    op.create_index("ix_device_tokens_user_id", "device_tokens", ["user_id"])
    op.add_column("notifications", sa.Column("last_attempt_at", sa.DateTime(timezone=True)))
    op.add_column("notifications", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("notifications", "next_attempt_at")
    op.drop_column("notifications", "last_attempt_at")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_table("device_tokens")
