"""Issued GST tax invoices are append-only.

A tax invoice (is_credit_note = false) and its lines can't be changed or deleted once
written; corrections go through credit notes. The one exception is invoices.pdf_url,
the cached private key of the rendered PDF. Credit notes keep their existing lifecycle
(a partial credit note is upgraded in place to the full reversal).

The app now computes an invoice's totals before inserting it, so nothing it does
updates a tax invoice row after the INSERT.

Revision ID: 0062_invoice_append_only
Revises: 0061_production_produced_qty
Create Date: 2026-11-01
"""
from alembic import op

revision = "0062_invoice_append_only"
down_revision = "0061_production_produced_qty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION drs_tax_invoice_immutable() RETURNS trigger AS $$
        BEGIN
            IF OLD.is_credit_note THEN
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'tax invoice % is append-only (no DELETE)', OLD.invoice_no
                    USING ERRCODE = 'check_violation';
            END IF;
            IF (to_jsonb(NEW) - 'pdf_url') IS DISTINCT FROM (to_jsonb(OLD) - 'pdf_url') THEN
                RAISE EXCEPTION 'tax invoice % is append-only (only pdf_url may change)',
                    OLD.invoice_no USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_invoices_append_only BEFORE UPDATE OR DELETE ON invoices "
        "FOR EACH ROW EXECUTE FUNCTION drs_tax_invoice_immutable();"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION drs_tax_invoice_line_immutable() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM invoices i
                       WHERE i.id = OLD.invoice_id AND NOT i.is_credit_note) THEN
                RAISE EXCEPTION 'lines of tax invoice % are append-only', OLD.invoice_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_invoice_lines_append_only BEFORE UPDATE OR DELETE ON invoice_lines "
        "FOR EACH ROW EXECUTE FUNCTION drs_tax_invoice_line_immutable();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_invoice_lines_append_only ON invoice_lines;")
    op.execute("DROP FUNCTION IF EXISTS drs_tax_invoice_line_immutable();")
    op.execute("DROP TRIGGER IF EXISTS trg_invoices_append_only ON invoices;")
    op.execute("DROP FUNCTION IF EXISTS drs_tax_invoice_immutable();")
