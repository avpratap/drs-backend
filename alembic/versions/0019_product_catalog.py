"""product category + image

Revision ID: 0019_product_catalog
Revises: 0018_append_only
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_product_catalog"
down_revision: str | None = "0018_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("category", sa.String(64)))
    op.add_column("products", sa.Column("image_url", sa.String(512)))
    op.create_index("ix_products_category", "products", ["category"])


def downgrade() -> None:
    op.drop_index("ix_products_category", table_name="products")
    op.drop_column("products", "image_url")
    op.drop_column("products", "category")
