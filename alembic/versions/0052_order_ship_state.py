"""orders.ship_state — per-order delivery state for authoritative GST place-of-supply.

The #M12 fix persisted delivery state only onto the (sticky, fill-once) Home shop, so a
repeat D2C customer or one with a different default Address was mis-taxed intra-state.
Storing the delivery state on the order itself and preferring it in place-of-supply makes
each order taxed by where IT ships (IGST vs CGST/SGST).

Revision ID: 0052_order_ship_state
Revises: 0051_batch_identity
Create Date: 2026-10-18
"""
import sqlalchemy as sa

from alembic import op

revision = "0052_order_ship_state"
down_revision = "0051_batch_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("ship_state", sa.String(120), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "ship_state")
