"""hardening: unique gateway_order_id + performance indexes

Revision ID: 0010_hardening
Revises: 0009_payments
Create Date: 2026-10-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_hardening"
down_revision: Union[str, None] = "0009_payments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One payment per gateway order (dedupe + safe single-row verify lookup).
    op.create_index(
        "uq_payments_gateway_order",
        "payments",
        ["gateway_order_id"],
        unique=True,
        postgresql_where=sa.text("gateway_order_id IS NOT NULL"),
    )
    # Hot-path indexes (nightly cutoff scan, order/payment lookups, price resolution).
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index("ix_orders_account_id", "orders", ["account_id"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_payment_status", "orders", ["payment_status"])
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index("ix_customer_accounts_owner", "customer_accounts", ["owner_user_id"])
    op.create_index(
        "ix_product_prices_lookup",
        "product_prices",
        ["product_id", "price_tier_id", "valid_from"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_prices_lookup", table_name="product_prices")
    op.drop_index("ix_customer_accounts_owner", table_name="customer_accounts")
    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_orders_payment_status", table_name="orders")
    op.drop_index("ix_orders_status", table_name="orders")
    op.drop_index("ix_orders_account_id", table_name="orders")
    op.drop_index("ix_subscriptions_status", table_name="subscriptions")
    op.drop_index("uq_payments_gateway_order", table_name="payments")
