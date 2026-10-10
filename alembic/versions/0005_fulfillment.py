"""proof_of_delivery, returns, cash_collections

Revision ID: 0005_fulfillment
Revises: 0004_delivery_orders
Create Date: 2026-10-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_fulfillment"
down_revision: Union[str, None] = "0004_delivery_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "proof_of_delivery",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("delivery_order_id", sa.BigInteger(), sa.ForeignKey("delivery_orders.id"), nullable=False),
        sa.Column("delivered_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("recipient_name", sa.String(255), nullable=True),
        sa.Column("photo_url", sa.String(512), nullable=True),
        sa.Column("signature_url", sa.String(512), nullable=True),
        sa.Column("lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("lng", sa.Numeric(9, 6), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_pod_order_id", "proof_of_delivery", ["delivery_order_id"])

    op.create_table(
        "returns",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("delivery_order_id", sa.BigInteger(), sa.ForeignKey("delivery_orders.id"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty_returned", sa.Numeric(10, 2), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_returns_order_id", "returns", ["delivery_order_id"])

    op.create_table(
        "cash_collections",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("delivery_order_id", sa.BigInteger(), sa.ForeignKey("delivery_orders.id"), nullable=False),
        sa.Column("amount_collected", sa.Numeric(12, 2), nullable=False),
        sa.Column("method", sa.String(16), server_default="cash", nullable=False),
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("collected_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cash_order_id", "cash_collections", ["delivery_order_id"])


def downgrade() -> None:
    op.drop_table("cash_collections")
    op.drop_table("returns")
    op.drop_table("proof_of_delivery")
