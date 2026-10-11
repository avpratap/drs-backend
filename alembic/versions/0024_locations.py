"""locations + per-location stock + reorder point

Revision ID: 0024_locations
Revises: 0023_invoice_pdf
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_locations"
down_revision: str | None = "0023_invoice_pdf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(16), server_default="warehouse", nullable=False),
        sa.Column("state", sa.String(120)),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Seed a default location and point existing stock at it.
    op.execute(
        "INSERT INTO locations (name, kind, is_default, status) "
        "VALUES ('Main Warehouse', 'warehouse', true, 'active')"
    )

    op.add_column("stock_items", sa.Column("location_id", sa.BigInteger(), sa.ForeignKey("locations.id")))
    op.add_column(
        "stock_items",
        sa.Column("reorder_point", sa.Numeric(12, 3), server_default="0", nullable=False),
    )
    op.execute(
        "UPDATE stock_items SET location_id = (SELECT id FROM locations WHERE is_default) "
        "WHERE location_id IS NULL"
    )
    op.alter_column("stock_items", "location_id", nullable=False)
    op.drop_constraint("uq_stock_product", "stock_items", type_="unique")
    op.create_unique_constraint(
        "uq_stock_product_location", "stock_items", ["product_id", "location_id"]
    )

    op.add_column("stock_movements", sa.Column("location_id", sa.BigInteger(), sa.ForeignKey("locations.id")))
    op.add_column("stock_movements", sa.Column("batch_id", sa.BigInteger()))


def downgrade() -> None:
    op.drop_column("stock_movements", "batch_id")
    op.drop_column("stock_movements", "location_id")
    op.drop_constraint("uq_stock_product_location", "stock_items", type_="unique")
    op.create_unique_constraint("uq_stock_product", "stock_items", ["product_id"])
    op.drop_column("stock_items", "reorder_point")
    op.drop_column("stock_items", "location_id")
    op.drop_table("locations")
