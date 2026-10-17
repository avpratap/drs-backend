"""one customer_account per owner_user_id (unique)

Revision ID: 0045_acct_owner_uq
Revises: 0044_churn
Create Date: 2026-10-17

The get-or-create account paths (orders/payments/shops/billing/loyalty) all assume a
user has exactly one CustomerAccount (model docstring: "a subscriber owns one account
that owns many shops"), but nothing enforced it — a concurrent first-touch could create
two, after which `scalar_one_or_none()` 500s that user forever. Enforce one-per-owner.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0045_acct_owner_uq"
down_revision: str | None = "0044_churn"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # De-dupe any pre-existing duplicates first (keep the lowest id per owner), so the
    # unique constraint can be created on a real DB that already raced.
    op.execute(
        """
        DELETE FROM customer_accounts a
        USING customer_accounts b
        WHERE a.owner_user_id = b.owner_user_id AND a.id > b.id
        """
    )
    op.create_unique_constraint(
        "uq_customer_accounts_owner", "customer_accounts", ["owner_user_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_customer_accounts_owner", "customer_accounts", type_="unique")
