"""admin depth: audit log, feature flags, outbound webhooks, product full-text index

Revision ID: 0041_admin_depth
Revises: 0040_notifications2
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041_admin_depth"
down_revision: str | None = "0040_notifications2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FTS = (
    "to_tsvector('english', coalesce(name,'')||' '||coalesce(sku,'')||' '"
    "||coalesce(description,''))"
)


def upgrade() -> None:
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("admin_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(48)),
        sa.Column("resource_id", sa.String(64)),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column("ip", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_admin_audit_admin", "admin_audit_log", ["admin_user_id"])
    op.create_index("ix_admin_audit_created", "admin_audit_log", ["created_at"])
    # Append-only (reuse drs_append_only() from 0018): audit rows are immutable.
    op.execute(
        "CREATE TRIGGER trg_admin_audit_log_append_only "
        "BEFORE UPDATE OR DELETE ON admin_audit_log "
        "FOR EACH ROW EXECUTE FUNCTION drs_append_only();"
    )

    op.create_table(
        "feature_flags",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("description", sa.String(255)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("key", name="uq_feature_flag_key"),
    )

    op.create_table(
        "outbound_webhooks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("secret", sa.String(128)),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_outbound_webhooks_event", "outbound_webhooks", ["event_type"])

    op.create_table(
        "outbound_webhook_deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("webhook_id", sa.BigInteger(), sa.ForeignKey("outbound_webhooks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("status", sa.String(12), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("response_code", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_webhook_deliveries_hook", "outbound_webhook_deliveries", ["webhook_id"])

    # GIN expression index for full-text product search.
    op.execute(f"CREATE INDEX ix_products_fts ON products USING gin ({_FTS})")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_products_fts")
    op.drop_index("ix_webhook_deliveries_hook", table_name="outbound_webhook_deliveries")
    op.drop_table("outbound_webhook_deliveries")
    op.drop_index("ix_outbound_webhooks_event", table_name="outbound_webhooks")
    op.drop_table("outbound_webhooks")
    op.drop_table("feature_flags")
    op.execute("DROP TRIGGER IF EXISTS trg_admin_audit_log_append_only ON admin_audit_log;")
    op.drop_index("ix_admin_audit_created", table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_admin", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
