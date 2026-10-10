"""auth_sessions table (refresh-token families)

Revision ID: 0002_auth_sessions
Revises: 0001_initial
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_auth_sessions"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("refresh_hash", sa.String(64), nullable=False),
        sa.Column("device_name", sa.String(255), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("revoked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("replaced_by", sa.BigInteger(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("refresh_hash", name="uq_auth_sessions_refresh_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_family_id", "auth_sessions", ["family_id"])
    op.create_index("ix_auth_sessions_refresh_hash", "auth_sessions", ["refresh_hash"])


def downgrade() -> None:
    op.drop_table("auth_sessions")
