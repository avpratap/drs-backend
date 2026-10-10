"""ARQ background worker — runs the daily cutoff on a schedule.

Run:  python -m uv run arq app.worker.WorkerSettings

The cron fires at 17:35 UTC = 23:05 IST (just after the 23:00 cutoff), so tomorrow's
standing orders are materialized into delivery_orders automatically every night.
ARQ cron times are UTC; adjust if CUTOFF_HOUR changes.
"""
import datetime as dt

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.db import SessionLocal
from app.services.cutoff import run_cutoff
from app.services.subscription import today_ist


async def scheduled_cutoff(ctx: dict) -> dict:
    delivery_date = today_ist() + dt.timedelta(days=1)
    async with SessionLocal() as db:
        result = await run_cutoff(db, delivery_date)
    result["delivery_date"] = str(result["delivery_date"])
    return result


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    functions = [scheduled_cutoff]
    cron_jobs = [cron(scheduled_cutoff, hour=17, minute=35)]  # 23:05 IST
