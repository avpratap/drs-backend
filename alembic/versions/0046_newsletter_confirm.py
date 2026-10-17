"""newsletter double opt-in: confirm_token

Revision ID: 0046_news_confirm
Revises: 0045_acct_owner_uq
Create Date: 2026-10-17

Adds a confirmation token so a subscribe only becomes a consented subscriber after
the real email owner confirms (prevents subscribing/forging consent for others).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046_news_confirm"
down_revision: str | None = "0045_acct_owner_uq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("newsletter_subscribers", sa.Column("confirm_token", sa.String(64)))


def downgrade() -> None:
    op.drop_column("newsletter_subscribers", "confirm_token")
