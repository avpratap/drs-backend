"""notification read_at (in-app inbox)

Revision ID: 0047_notif_read
Revises: 0046_news_confirm
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0047_notif_read"
down_revision: str | None = "0046_news_confirm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("read_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("notifications", "read_at")
