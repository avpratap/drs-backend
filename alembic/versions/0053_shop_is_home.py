"""shops.is_home — flag the account's dedicated D2C delivery location.

Replaces selecting the D2C home shop by the magic name "Home" (which could hijack a B2B
store literally named "Home"). Backfill only RETAIL accounts' "Home" shops so existing
auto-provisioned home shops keep working; a B2B store named "Home" stays is_home=false.

Revision ID: 0053_shop_is_home
Revises: 0052_order_ship_state
Create Date: 2026-10-18
"""
import sqlalchemy as sa

from alembic import op

revision = "0053_shop_is_home"
down_revision = "0052_order_ship_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shops",
        sa.Column("is_home", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        "UPDATE shops SET is_home = true WHERE name = 'Home' AND account_id IN "
        "(SELECT id FROM customer_accounts WHERE account_type = 'retail')"
    )


def downgrade() -> None:
    op.drop_column("shops", "is_home")
