"""inventory: stock items + append-only movement ledger

Revision ID: 0020_inventory
Revises: 0019_product_catalog
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_inventory"
down_revision: str | None = "0019_product_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty_on_hand", sa.Numeric(12, 3), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("product_id", name="uq_stock_product"),
    )
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("change", sa.Numeric(12, 3), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("ref_type", sa.String(24)),
        sa.Column("ref_id", sa.BigInteger()),
        sa.Column("note", sa.String(255)),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_stock_movements_product_id", "stock_movements", ["product_id"])
    # Movement ledger is append-only (reuses the trigger fn from 0018).
    op.execute(
        "CREATE TRIGGER trg_stock_movements_append_only "
        "BEFORE UPDATE OR DELETE ON stock_movements "
        "FOR EACH ROW EXECUTE FUNCTION drs_append_only();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_stock_movements_append_only ON stock_movements;")
    op.drop_index("ix_stock_movements_product_id", table_name="stock_movements")
    op.drop_table("stock_movements")
    op.drop_table("stock_items")
