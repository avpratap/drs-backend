"""delivery ops: driver locations + delivery_order failure reason

Revision ID: 0042_delivery_ops
Revises: 0041_admin_depth
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042_delivery_ops"
down_revision: str | None = "0041_admin_depth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("delivery_orders", sa.Column("failure_reason", sa.String(32)))
    op.create_table(
        "driver_locations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("driver_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("lat", sa.Numeric(9, 6), nullable=False),
        sa.Column("lng", sa.Numeric(9, 6), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_driver_locations_driver_id", "driver_locations", ["driver_id"])


def downgrade() -> None:
    op.drop_index("ix_driver_locations_driver_id", table_name="driver_locations")
    op.drop_table("driver_locations")
    op.drop_column("delivery_orders", "failure_reason")
