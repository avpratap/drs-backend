"""stock transfers: header + items + dispatched-lot snapshot

Revision ID: 0027_transfers
Revises: 0026_procurement
Create Date: 2026-10-16
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_transfers"
down_revision: str | None = "0026_procurement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_transfers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("transfer_no", sa.String(32), nullable=False),
        sa.Column("from_location_id", sa.BigInteger(), sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("to_location_id", sa.BigInteger(), sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("note", sa.String(255)),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("transfer_no", name="uq_transfer_no"),
    )
    op.create_table(
        "stock_transfer_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("transfer_id", sa.BigInteger(), sa.ForeignKey("stock_transfers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty", sa.Numeric(12, 3), nullable=False),
    )
    op.create_index("ix_transfer_items_transfer_id", "stock_transfer_items", ["transfer_id"])
    op.create_table(
        "stock_transfer_lots",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("transfer_item_id", sa.BigInteger(), sa.ForeignKey("stock_transfer_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_no", sa.String(64)),
        sa.Column("mfg_date", sa.Date()),
        sa.Column("expiry_date", sa.Date()),
        sa.Column("qty", sa.Numeric(12, 3), nullable=False),
    )
    op.create_index("ix_transfer_lots_item_id", "stock_transfer_lots", ["transfer_item_id"])


def downgrade() -> None:
    op.drop_table("stock_transfer_lots")
    op.drop_index("ix_transfer_items_transfer_id", table_name="stock_transfer_items")
    op.drop_table("stock_transfer_items")
    op.drop_table("stock_transfers")
