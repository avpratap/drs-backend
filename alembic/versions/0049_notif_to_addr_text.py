"""Widen notifications.to_addr to Text — a push row joins all of a user's device tokens
(each up to 512 chars), which overflowed VARCHAR(255) and silently dropped the push.

Revision ID: 0049_notif_toaddr
Revises: 0048_inv_order_uq
Create Date: 2026-10-17
"""
import sqlalchemy as sa

from alembic import op

revision = "0049_notif_toaddr"
down_revision = "0048_inv_order_uq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "notifications", "to_addr",
        existing_type=sa.String(255), type_=sa.Text(), existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "notifications", "to_addr",
        existing_type=sa.Text(), type_=sa.String(255), existing_nullable=True,
    )
