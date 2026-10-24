"""Invoice integrity: one tax invoice per one-time order, and separate consecutive
per-FY serial series for tax invoices vs credit notes.

Revision ID: 0048_inv_order_uq
Revises: 0047_notif_read
Create Date: 2026-10-17
"""
import sqlalchemy as sa

from alembic import op

revision = "0048_inv_order_uq"
down_revision = "0047_notif_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One tax invoice per one-time order (mirrors the delivery-order partial-unique index).
    op.create_index(
        "uq_invoice_order",
        "invoices",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("order_id IS NOT NULL AND is_credit_note = false"),
    )
    # seq_no is now consecutive PER document type, so uniqueness must include the type
    # (tax invoice seq 1 and credit-note seq 1 are distinct rows).
    op.drop_constraint("uq_invoice_fy_seq", "invoices", type_="unique")
    op.create_unique_constraint(
        "uq_invoice_fy_seq_type", "invoices", ["fy", "seq_no", "is_credit_note"]
    )


def downgrade() -> None:
    # Credit notes have their own serial series since this revision, so (fy, seq_no)
    # overlaps with invoices once any credit note exists: fail with a clear message
    # rather than a bare unique-violation half way through the downgrade.
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM invoices GROUP BY fy, seq_no HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'cannot downgrade 0048: invoice and credit-note serials overlap';
          END IF;
        END $$;
        """
    )
    op.drop_constraint("uq_invoice_fy_seq_type", "invoices", type_="unique")
    op.create_unique_constraint("uq_invoice_fy_seq", "invoices", ["fy", "seq_no"])
    op.drop_index("uq_invoice_order", table_name="invoices")
