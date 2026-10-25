"""Re-run the 0058 retail min_qty backfill (idempotent), e.g. after adding a smaller pack.

Run:  uv run python scripts/backfill_retail_min_qty.py
"""
import asyncio
import importlib.util
from pathlib import Path

from sqlalchemy import text

_MIGRATION = Path(__file__).resolve().parents[1] / "alembic/versions/0058_backfill_retail_min_qty.py"


def backfill_sql() -> str:
    """The migration's SQL — one source of truth for the upgrade and this command."""
    spec = importlib.util.spec_from_file_location("backfill_0058", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BACKFILL_SQL


async def run(engine=None) -> int:
    """Apply the backfill; returns the number of price rows lowered."""
    if engine is None:
        from app.core.db import engine
    async with engine.begin() as conn:
        return (await conn.execute(text(backfill_sql()))).rowcount


if __name__ == "__main__":
    print(f"retail price rows corrected: {asyncio.run(run())}")
