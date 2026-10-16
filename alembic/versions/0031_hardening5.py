"""hardening: stock on-hand floor + per-location stock index

Revision ID: 0031_hardening5
Revises: 0030_hardening4
Create Date: 2026-10-17

- Floor CHECK on stock_items.qty_on_hand (mirrors the wallet balance floor): stock
  may go negative (a delivery is never blocked on inventory) but not unboundedly,
  which would signal a bug.
- Composite index (location_id, product_id) for per-location stock scans (the
  existing unique is (product_id, location_id), which doesn't serve location-first
  queries well).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0031_hardening5"
down_revision: str | None = "0030_hardening4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_stock_qty_floor", "stock_items", "qty_on_hand >= -1000000"
    )
    op.create_index(
        "ix_stock_items_location_product", "stock_items", ["location_id", "product_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_stock_items_location_product", table_name="stock_items")
    op.drop_constraint("ck_stock_qty_floor", "stock_items", type_="check")
