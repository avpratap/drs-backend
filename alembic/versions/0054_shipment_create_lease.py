"""shipments.create_lease_until — in-flight claim on a draft shipment (S1).

create_shipment no longer holds a row lock across the courier HTTP call: it commits a
'draft' first, calls Shiprocket with no lock held, then finalizes. This lease marks a
draft whose courier call is in flight, so a concurrent/retried create can't fire a
second courier order for it; an expired (or cleared) lease means the draft is resumable.

Revision ID: 0054_shipment_create_lease
Revises: 0053_shop_is_home
Create Date: 2026-10-24
"""
import sqlalchemy as sa

from alembic import op

revision = "0054_shipment_create_lease"
down_revision = "0053_shop_is_home"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shipments",
        sa.Column("create_lease_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shipments", "create_lease_until")
