"""DB-enforced append-only ledgers (reject UPDATE/DELETE)

Revision ID: 0018_append_only
Revises: 0017_shop_state
Create Date: 2026-10-11
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0018_append_only"
down_revision: str | None = "0017_shop_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("wallet_transactions", "credit_ledger", "consents")


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION drs_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ledger table is append-only (no UPDATE/DELETE)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for t in _TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{t}_append_only "
            f"BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION drs_append_only();"
        )


def downgrade() -> None:
    for t in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_append_only ON {t};")
    op.execute("DROP FUNCTION IF EXISTS drs_append_only();")
