"""Backfill retail-tier price min_qty down to each product's smallest pack.

Retail price rows created before the API began defaulting min_qty to the smallest pack
were stored at min_qty 1, so every sub-1 pack (0.5 L, 200 g) resolved no RETAIL price
("price unavailable"). For each retail-tier (tier_type = 'retail') price VERSION —
(product, tier, valid_from) — its BASE rung (lowest min_qty) is lowered to the product's
smallest ACTIVE variant pack_size (else its own pack_size), rounded DOWN to the column's
2 dp (floor 0.01), when it is higher than that.

Deliberately narrow:
- only the base rung, and only when it is <= 1 (the old default): a real volume break
  (e.g. min_qty 10) is never flattened into a base price;
- never raises a min_qty; B2B / other tiers untouched.
Lowering the base rung can't hit uq_product_price: any row already at the target
min_qty in that version would itself be the (lower) base rung.

Idempotent, self-contained SQL; `scripts/backfill_retail_min_qty.py` re-runs it (e.g.
after a smaller variant is added). Downgrade is a no-op: the old values were a defect.

Revision ID: 0058_backfill_retail_min_qty
Revises: 0057_location_name_unique
Create Date: 2026-10-25
"""
from alembic import op

revision = "0058_backfill_retail_min_qty"
down_revision = "0057_location_name_unique"
branch_labels = None
depends_on = None

BACKFILL_SQL = """
WITH target AS (
    SELECT p.id AS product_id,
           GREATEST(
               floor(COALESCE(
                   (SELECT min(v.pack_size) FROM product_variants v
                     WHERE v.product_id = p.id AND v.status = 'active'),
                   p.pack_size
               ) * 100) / 100,
               0.01
           ) AS min_qty
      FROM products p
),
base AS (
    SELECT DISTINCT ON (pp.product_id, pp.price_tier_id, pp.valid_from)
           pp.id, pp.product_id, pp.min_qty
      FROM product_prices pp
      JOIN price_tiers t ON t.id = pp.price_tier_id
     WHERE t.tier_type = 'retail'
     ORDER BY pp.product_id, pp.price_tier_id, pp.valid_from, pp.min_qty, pp.id
)
UPDATE product_prices pp
   SET min_qty = target.min_qty, updated_at = now()
  FROM base
  JOIN target ON target.product_id = base.product_id
 WHERE pp.id = base.id
   AND base.min_qty > target.min_qty
   AND base.min_qty <= 1
"""


def upgrade() -> None:
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    pass  # data fix: the previous min_qty values are what made packs unpriceable
