"""ARQ background worker — runs the daily cutoff on a schedule.

Run:  python -m uv run arq app.worker.WorkerSettings

The cron fires at 17:35 UTC = 23:05 IST (just after the 23:00 cutoff), so tomorrow's
standing orders are materialized into delivery_orders automatically every night.
ARQ cron times are UTC; adjust if CUTOFF_HOUR changes.
"""
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from arq import Retry, cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.db import SessionLocal
from app.services.cutoff import run_cutoff
from app.services.loyalty import sweep_unrewarded_referrals
from app.services.maintenance import prune
from app.services.ml import churn as churn_svc
from app.services.notifications import process_outbox
from app.services.subscription import today_ist

log = logging.getLogger("drs.worker")


def _scheduled_day(ctx: dict) -> dt.date:
    """The IST day this job was SCHEDULED for (arq's enqueue_time), not "now": a retry
    that lands after midnight must still build the same delivery date, not skip a day."""
    enq = ctx.get("enqueue_time")
    if isinstance(enq, dt.datetime):
        if enq.tzinfo is None:
            enq = enq.replace(tzinfo=dt.UTC)
        return enq.astimezone(ZoneInfo(settings.TIMEZONE)).date()
    return today_ist()


async def scheduled_cutoff(ctx: dict) -> dict:
    delivery_date = _scheduled_day(ctx) + dt.timedelta(days=1)
    try:
        async with SessionLocal() as db:
            result = await run_cutoff(db, delivery_date)
    except Exception as exc:
        # arq only auto-retries on an explicit Retry, not a plain exception. run_cutoff is
        # idempotent, so re-raise as Retry (bounded by max_tries) with backoff so a
        # transient DB blip doesn't silently skip tomorrow's standing orders.
        attempt = ctx.get("job_try", 1)
        log.warning("scheduled_cutoff attempt %s failed: %s", attempt, exc)
        raise Retry(defer=min(60 * attempt, 300)) from exc
    if result.get("failed"):
        # Some subscriptions failed (logged per sub) — the run is 'partial'. Rerun is
        # idempotent, so retry rather than silently leaving those shops undelivered.
        attempt = ctx.get("job_try", 1)
        log.error(
            "scheduled_cutoff attempt %s: %s subscription(s) failed for %s",
            attempt, result["failed"], delivery_date,
        )
        if attempt < 4:
            raise Retry(defer=min(60 * attempt, 300))
    result["delivery_date"] = str(result["delivery_date"])
    return result


async def drain_outbox(ctx: dict) -> dict:
    """Deliver due queued notifications. Runs every minute."""
    async with SessionLocal() as db:
        return await process_outbox(db, limit=200)


async def housekeeping(ctx: dict) -> dict:
    """Prune short-retention rows (idempotency keys, webhook events, old notifications)."""
    async with SessionLocal() as db:
        counts = await prune(db)
        # Referral rewards a redeem/payment race left unpaid.
        counts["referrals_rewarded"] = await sweep_unrewarded_referrals(db)
        return counts


async def score_churn(ctx: dict) -> dict:
    """Nightly churn scoring over all accounts (ML on our infra; aggregate data only)."""
    async with SessionLocal() as db:
        return await churn_svc.score_all(db)


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    functions = [scheduled_cutoff, drain_outbox, housekeeping, score_churn]
    # arq's cron() takes PER-JOB max_tries/keep_result that OVERRIDE the worker-level
    # settings (and default to max_tries=1, keep_result=0), so they must be set here or
    # the "retry + 1h result retention" below would NOT apply to these scheduled jobs.
    cron_jobs = [
        # Per-job timeout sized for the batch (the worker default below is for short jobs).
        cron(
            scheduled_cutoff, hour=17, minute=35, max_tries=4, keep_result=3600, timeout=3000
        ),  # 23:05 IST
        cron(drain_outbox, max_tries=2, keep_result=300),  # every minute
        cron(housekeeping, hour=1, minute=0, max_tries=3, keep_result=3600),  # daily prune
        cron(score_churn, hour=2, minute=0, max_tries=3, keep_result=3600),  # nightly churn
    ]
    # Resilience: retry failed jobs with backoff; a job that exhausts max_tries
    # lands in ARQ's result store with its error (our dead-letter surface).
    max_tries = 4
    retry_jobs = True
    job_timeout = 600  # seconds; a stuck cutoff is killed, not hung forever
    keep_result = 3600  # keep job results 1h for inspection
    max_jobs = 20
