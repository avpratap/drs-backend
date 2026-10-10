"""notifications outbox

Revision ID: 0012_notifications
Revises: 0011_profile
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_notifications"
down_revision: str | None = "0011_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id")),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("channel", sa.String(12), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("template_code", sa.String(64)),
        sa.Column("to_addr", sa.String(255)),
        sa.Column("body", sa.Text()),
        sa.Column("payload", sa.JSON()),
        sa.Column("status", sa.String(12), server_default="queued", nullable=False),
        sa.Column("skip_reason", sa.String(48)),
        sa.Column("provider", sa.String(16)),
        sa.Column("provider_message_id", sa.String(128)),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dedupe_key", sa.String(128)),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_status", "notifications", ["status"])
    # Worker pulls due 'queued' rows ordered by schedule.
    op.create_index(
        "ix_notifications_due", "notifications", ["status", "scheduled_for"]
    )
    # Idempotent emits: at most one row per (dedupe_key, channel).
    op.create_index(
        "uq_notifications_dedupe",
        "notifications",
        ["dedupe_key", "channel"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_notifications_dedupe", table_name="notifications")
    op.drop_index("ix_notifications_due", table_name="notifications")
    op.drop_index("ix_notifications_status", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")
