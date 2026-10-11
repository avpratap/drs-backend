"""invoice pdf_url

Revision ID: 0023_invoice_pdf
Revises: 0022_ratings_wd
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_invoice_pdf"
down_revision: str | None = "0022_ratings_wd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("pdf_url", sa.String(512)))


def downgrade() -> None:
    op.drop_column("invoices", "pdf_url")
