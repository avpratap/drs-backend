"""hardening: non-negative money CHECKs, immutable webhook_events, missing indexes

Revision ID: 0030_hardening4
Revises: 0029_hardening3
Create Date: 2026-10-17

Tier-2 hardening from the deep audit:
  * Non-negative money CHECK constraints on order / delivery / PO line items and
    order totals. (invoice_lines are intentionally excluded — credit notes carry
    negative amounts; invoices already guard `total >= 0 OR is_credit_note`.)
  * webhook_events made immutable (block UPDATE) so a recorded gateway event can
    never be altered. DELETE stays allowed — housekeeping prunes old events.
  * Indexes on invoices.order_id and payment_mandates.payment_method_id (FKs that
    were filtered/joined but unindexed).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0030_hardening4"
down_revision: str | None = "0029_hardening3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY_CHECKS = [
    ("ck_orders_amounts_nonneg", "orders", "subtotal >= 0 AND discount >= 0 AND total >= 0"),
    ("ck_order_items_price_nonneg", "order_items", "unit_price >= 0 AND line_total >= 0"),
    ("ck_delivery_items_price_nonneg", "delivery_order_items", "unit_price >= 0 AND line_total >= 0"),
    ("ck_po_items_cost_nonneg", "purchase_order_items", "unit_cost >= 0 AND line_total >= 0"),
]


def upgrade() -> None:
    for name, table, cond in _MONEY_CHECKS:
        op.create_check_constraint(name, table, cond)

    op.execute(
        """
        CREATE OR REPLACE FUNCTION drs_no_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'row is immutable (no UPDATE)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_webhook_events_no_update "
        "BEFORE UPDATE ON webhook_events "
        "FOR EACH ROW EXECUTE FUNCTION drs_no_update();"
    )

    op.create_index("ix_invoices_order_id", "invoices", ["order_id"])
    op.create_index(
        "ix_payment_mandates_payment_method_id", "payment_mandates", ["payment_method_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_payment_mandates_payment_method_id", table_name="payment_mandates")
    op.drop_index("ix_invoices_order_id", table_name="invoices")
    op.execute("DROP TRIGGER IF EXISTS trg_webhook_events_no_update ON webhook_events;")
    op.execute("DROP FUNCTION IF EXISTS drs_no_update();")
    for name, table, _cond in _MONEY_CHECKS:
        op.drop_constraint(name, table, type_="check")
