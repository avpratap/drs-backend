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
from app.services.maintenance import prune
from app.services.ml import churn as churn_svc
from app.services.notifications import process_outbox
from app.services.subscription import today_ist


async def scheduled_cutoff(ctx: dict) -> dict:
    delivery_date = today_ist() + dt.timedelta(days=1)
    async with SessionLocal() as db:
        result = await run_cutoff(db, delivery_date)
    result["delivery_date"] = str(result["delivery_date"])
    return result


async def drain_outbox(ctx: dict) -> dict:
    """Deliver due queued notifications. Runs every minute."""
    async with SessionLocal() as db:
        return await process_outbox(db, limit=200)


async def housekeeping(ctx: dict) -> dict:
    """Prune short-retention rows (idempotency keys, webhook events, old notifications)."""
    async with SessionLocal() as db:
        return await prune(db)


async def score_churn(ctx: dict) -> dict:
    """Nightly churn scoring over all accounts (ML on our infra; aggregate data only)."""
    async with SessionLocal() as db:
        return await churn_svc.score_all(db)


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    functions = [scheduled_cutoff, drain_outbox, housekeeping, score_churn]
    cron_jobs = [
        cron(scheduled_cutoff, hour=17, minute=35),  # 23:05 IST
        cron(drain_outbox),  # every minute
        cron(housekeeping, hour=1, minute=0),  # daily prune
        cron(score_churn, hour=2, minute=0),  # nightly churn scoring
    ]
    # Resilience: retry failed jobs with backoff; a job that exhausts max_tries
    # lands in ARQ's result store with its error (our dead-letter surface).
    max_tries = 4
    retry_jobs = True
    job_timeout = 600  # seconds; a stuck cutoff is killed, not hung forever
    keep_result = 3600  # keep job results 1h for inspection
    max_jobs = 20
