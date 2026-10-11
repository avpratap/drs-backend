"""stock batches/lots + expiry (FEFO)

Revision ID: 0025_batches
Revises: 0024_locations
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_batches"
down_revision: str | None = "0024_locations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_batches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("location_id", sa.BigInteger(), sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("batch_no", sa.String(64), nullable=False),
        sa.Column("mfg_date", sa.Date()),
        sa.Column("expiry_date", sa.Date()),
        sa.Column("qty", sa.Numeric(12, 3), nullable=False),
        sa.Column("status", sa.String(12), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("qty >= 0", name="ck_batch_qty_nonneg"),
    )
    op.create_index("ix_stock_batches_product_id", "stock_batches", ["product_id"])
    op.create_index(
        "ix_stock_batches_fefo", "stock_batches", ["product_id", "location_id", "expiry_date"]
    )
    # Now that the table exists, add the FK the movement ledger pointed at in Wave A.
    op.create_foreign_key(
        "fk_stock_movements_batch", "stock_movements", "stock_batches", ["batch_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_stock_movements_batch", "stock_movements", type_="foreignkey")
    op.drop_index("ix_stock_batches_fefo", table_name="stock_batches")
    op.drop_index("ix_stock_batches_product_id", table_name="stock_batches")
    op.drop_table("stock_batches")
