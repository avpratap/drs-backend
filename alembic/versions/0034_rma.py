"""customer returns / RMA

Revision ID: 0034_rma
Revises: 0033_carts
Create Date: 2026-10-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_rma"
down_revision: str | None = "0033_carts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "return_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("rma_no", sa.String(32), nullable=False),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("customer_accounts.id"), nullable=False),
        sa.Column("status", sa.String(16), server_default="requested", nullable=False),
        sa.Column("reason", sa.String(255)),
        sa.Column("resolution", sa.String(255)),
        sa.Column("refund_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("rma_no", name="uq_rma_no"),
    )
    op.create_index("ix_return_requests_order_id", "return_requests", ["order_id"])
    op.create_index("ix_return_requests_account_id", "return_requests", ["account_id"])
    op.create_table(
        "return_request_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("return_request_id", sa.BigInteger(), sa.ForeignKey("return_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty", sa.Numeric(12, 3), nullable=False),
        sa.Column("reason", sa.String(255)),
    )
    op.create_index("ix_rma_items_request_id", "return_request_items", ["return_request_id"])


def downgrade() -> None:
    op.drop_index("ix_rma_items_request_id", table_name="return_request_items")
    op.drop_table("return_request_items")
    op.drop_index("ix_return_requests_account_id", table_name="return_requests")
    op.drop_index("ix_return_requests_order_id", table_name="return_requests")
    op.drop_table("return_requests")
