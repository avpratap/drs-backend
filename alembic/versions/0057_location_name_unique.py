"""Location names are unique (case- and surrounding-space-insensitive).

Two "Main Warehouse" rows made stock reports and transfer pickers ambiguous. Existing
duplicates are renamed deterministically before the index is created: the oldest row
(lowest id) keeps its name, every later one becomes "<name> (#<id>)". Nothing else about
a renamed location changes (stock, batches and movements reference it by id).

Revision ID: 0057_location_name_unique
Revises: 0056_review_round2
Create Date: 2026-10-25
"""
import sqlalchemy as sa

from alembic import op

revision = "0057_location_name_unique"
down_revision = "0056_review_round2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trim stray whitespace first so " Main Warehouse" and "Main Warehouse" collide too.
    op.execute("UPDATE locations SET name = btrim(name) WHERE name <> btrim(name)")
    # The suffix carries the row id, so renamed names can't collide with each other; the
    # 120-char column is respected by truncating the base name.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, name,
                   row_number() OVER (PARTITION BY lower(name) ORDER BY id) AS rn
            FROM locations
        )
        UPDATE locations AS l
        SET name = left(r.name, 120 - length(' (#' || l.id || ')')) || ' (#' || l.id || ')'
        FROM ranked AS r
        WHERE r.id = l.id AND r.rn > 1
        """
    )
    op.create_index("uq_locations_name_ci", "locations", [sa.text("lower(name)")], unique=True)


def downgrade() -> None:
    # Renamed rows keep their new names (renaming them back would recreate the ambiguity).
    op.drop_index("uq_locations_name_ci", table_name="locations")
