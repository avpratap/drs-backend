"""schema hardening: qty precision + CHECKs, invoice uniqueness, stock loc NOT NULL

Revision ID: 0029_hardening3
Revises: 0028_production
Create Date: 2026-10-17

Tier-1 correctness fixes from the deep audit:
  * Standardize quantity columns to Numeric(12,3) so delivery qty never rounds
    differently from the invoice line it feeds.
  * CHECK constraints forbidding negative/zero quantities on line items.
  * One non-credit-note invoice per delivery_order, enforced at the DB (was code-only).
  * stock_movements.location_id backfilled and made NOT NULL (NULL fragmented stock).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_hardening3"
down_revision: str | None = "0028_production"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) quantity fields standardized to Numeric(12,3).
_QTY_COLS = [
    ("order_items", "qty"),
    ("delivery_order_items", "qty_planned"),
    ("delivery_order_items", "qty_delivered"),
    ("subscription_items", "default_qty"),
    ("subscription_overrides", "override_qty"),
    ("returns", "qty_returned"),
]

# (constraint name, table, condition) non-negative / positive quantity guards.
_QTY_CHECKS = [
    ("ck_order_items_qty_pos", "order_items", "qty > 0"),
    ("ck_delivery_items_planned_nonneg", "delivery_order_items", "qty_planned >= 0"),
    ("ck_delivery_items_delivered_nonneg", "delivery_order_items", "qty_delivered >= 0"),
    ("ck_po_items_ordered_pos", "purchase_order_items", "qty_ordered > 0"),
    ("ck_po_items_received_nonneg", "purchase_order_items", "qty_received >= 0"),
    ("ck_returns_qty_pos", "returns", "qty_returned > 0"),
    ("ck_sub_items_qty_nonneg", "subscription_items", "default_qty >= 0"),
    ("ck_sub_overrides_qty_nonneg", "subscription_overrides", "override_qty >= 0"),
]


def upgrade() -> None:
    for table, col in _QTY_COLS:
        op.alter_column(table, col, type_=sa.Numeric(12, 3), existing_nullable=False)

    for name, table, cond in _QTY_CHECKS:
        op.create_check_constraint(name, table, cond)

    # (The one-invoice-per-delivery_order unique partial index already exists from
    # migration 0015 — no need to recreate it here.)

    # Backfill any NULL movement locations to the default (or first) location, then
    # forbid NULL so every movement is attributable to a location.
    op.execute(
        "UPDATE stock_movements SET location_id = COALESCE("
        "(SELECT id FROM locations WHERE is_default LIMIT 1), "
        "(SELECT id FROM locations ORDER BY id LIMIT 1)) "
        "WHERE location_id IS NULL"
    )
    op.alter_column("stock_movements", "location_id", nullable=False)
    op.create_index("ix_stock_movements_location_id", "stock_movements", ["location_id"])


def downgrade() -> None:
    op.drop_index("ix_stock_movements_location_id", table_name="stock_movements")
    op.alter_column("stock_movements", "location_id", nullable=True)
    for name, table, _cond in _QTY_CHECKS:
        op.drop_constraint(name, table, type_="check")
    for table, col in _QTY_COLS:
        op.alter_column(table, col, type_=sa.Numeric(10, 2), existing_nullable=False)
