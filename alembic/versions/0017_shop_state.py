"""shop state/pincode for GST place of supply

Revision ID: 0017_shop_state
Revises: 0016_hardening2
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_shop_state"
down_revision: str | None = "0016_hardening2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shops", sa.Column("state", sa.String(120)))
    op.add_column("shops", sa.Column("pincode", sa.String(10)))


def downgrade() -> None:
    op.drop_column("shops", "pincode")
    op.drop_column("shops", "state")
