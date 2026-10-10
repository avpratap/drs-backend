"""Run the daily cutoff from the CLI (scheduled by ARQ/Celery Beat in prod).

Usage:  python -m uv run python -m app.jobs.cutoff [YYYY-MM-DD]   (default: tomorrow)
"""
import asyncio
import datetime as dt
import sys

from app.core.db import SessionLocal
from app.services.cutoff import run_cutoff
from app.services.subscription import today_ist


async def main() -> None:
    if len(sys.argv) > 1:
        delivery_date = dt.date.fromisoformat(sys.argv[1])
    else:
        delivery_date = today_ist() + dt.timedelta(days=1)
    async with SessionLocal() as db:
        result = await run_cutoff(db, delivery_date)
    print("Cutoff run:", result)


if __name__ == "__main__":
    asyncio.run(main())
