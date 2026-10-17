"""support tickets + threaded messages

Revision ID: 0036_support
Revises: 0035_reviews
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_support"
down_revision: str | None = "0035_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ticket_no", sa.String(32), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id")),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("category", sa.String(16), server_default="other", nullable=False),
        sa.Column("status", sa.String(12), server_default="open", nullable=False),
        sa.Column("priority", sa.String(8), server_default="normal", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("ticket_no", name="uq_ticket_no"),
    )
    op.create_index("ix_support_tickets_created_by", "support_tickets", ["created_by"])
    op.create_index("ix_support_tickets_account_id", "support_tickets", ["account_id"])
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"])

    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ticket_id", sa.BigInteger(), sa.ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("sender_role", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("attachment_url", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ticket_messages_ticket_id", "ticket_messages", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_messages_ticket_id", table_name="ticket_messages")
    op.drop_table("ticket_messages")
    op.drop_index("ix_support_tickets_status", table_name="support_tickets")
    op.drop_index("ix_support_tickets_account_id", table_name="support_tickets")
    op.drop_index("ix_support_tickets_created_by", table_name="support_tickets")
    op.drop_table("support_tickets")
