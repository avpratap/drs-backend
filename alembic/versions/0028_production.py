"""production: bills of materials + components + production orders

Revision ID: 0028_production
Revises: 0027_transfers
Create Date: 2026-10-16
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_production"
down_revision: str | None = "0027_transfers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bills_of_materials",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("output_qty", sa.Numeric(12, 3), server_default="1", nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bom_product_id", "bills_of_materials", ["product_id"])
    op.create_table(
        "bom_components",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("bom_id", sa.BigInteger(), sa.ForeignKey("bills_of_materials.id", ondelete="CASCADE"), nullable=False),
        sa.Column("component_product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty", sa.Numeric(12, 3), nullable=False),
    )
    op.create_index("ix_bom_components_bom_id", "bom_components", ["bom_id"])
    op.create_table(
        "production_orders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("pro_no", sa.String(32), nullable=False),
        sa.Column("bom_id", sa.BigInteger(), sa.ForeignKey("bills_of_materials.id"), nullable=False),
        sa.Column("product_id", sa.BigInteger(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("location_id", sa.BigInteger(), sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("planned_qty", sa.Numeric(12, 3), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("output_batch_no", sa.String(64)),
        sa.Column("mfg_date", sa.Date()),
        sa.Column("expiry_date", sa.Date()),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("pro_no", name="uq_pro_no"),
    )
    op.create_index("ix_production_orders_bom_id", "production_orders", ["bom_id"])


def downgrade() -> None:
    op.drop_table("production_orders")
    op.drop_index("ix_bom_components_bom_id", table_name="bom_components")
    op.drop_table("bom_components")
    op.drop_index("ix_bom_product_id", table_name="bills_of_materials")
    op.drop_table("bills_of_materials")
