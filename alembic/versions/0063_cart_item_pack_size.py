"""Cart items remember the chosen pack.

cart_items gains pack_size (base units per pack the customer picked). qty stays in base
units and is a whole number of pack_size. The uniqueness moves from (cart, product) to
(cart, product, pack_size) so the same product in two pack sizes is two independent lines.

Existing rows are backfilled with the product's smallest active variant pack (the size the
old UI assumed), falling back to the product's own pack_size, then 1.

Revision ID: 0063_cart_item_pack_size
Revises: 0062_invoice_append_only
Create Date: 2026-11-15
"""
import sqlalchemy as sa

from alembic import op

revision = "0063_cart_item_pack_size"
down_revision = "0062_invoice_append_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cart_items", sa.Column("pack_size", sa.Numeric(10, 3), nullable=True))
    # Backfill: smallest active variant pack, else the product's own pack_size, else the qty.
    op.execute(
        """
        UPDATE cart_items ci SET pack_size = COALESCE(
            (SELECT MIN(pv.pack_size) FROM product_variants pv
              WHERE pv.product_id = ci.product_id AND pv.status = 'active' AND pv.pack_size > 0),
            (SELECT p.pack_size FROM products p WHERE p.id = ci.product_id),
            ci.qty
        )
        WHERE ci.pack_size IS NULL;
        """
    )
    # Any still unresolved (no product row, zero/negative) -> 1 so the column is clean.
    op.execute("UPDATE cart_items SET pack_size = 1 WHERE pack_size IS NULL OR pack_size <= 0;")
    op.alter_column("cart_items", "pack_size", nullable=False)
    op.drop_constraint("uq_cart_items_cart_product", "cart_items", type_="unique")
    op.create_unique_constraint(
        "uq_cart_items_cart_product_pack", "cart_items", ["cart_id", "product_id", "pack_size"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_cart_items_cart_product_pack", "cart_items", type_="unique")
    # Collapse any per-(cart, product) duplicates back into one line (sum qty onto the
    # lowest id) before restoring the narrower unique constraint.
    op.execute(
        """
        UPDATE cart_items c SET qty = s.total
        FROM (
            SELECT MIN(id) AS keep_id, SUM(qty) AS total
            FROM cart_items GROUP BY cart_id, product_id
        ) s
        WHERE c.id = s.keep_id;
        """
    )
    op.execute(
        """
        DELETE FROM cart_items WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY cart_id, product_id ORDER BY id
                ) AS rn FROM cart_items
            ) t WHERE t.rn > 1
        );
        """
    )
    op.create_unique_constraint(
        "uq_cart_items_cart_product", "cart_items", ["cart_id", "product_id"]
    )
    op.drop_column("cart_items", "pack_size")
