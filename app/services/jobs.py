"""Scheduled jobs runnable WITHOUT the ARQ worker: by the API's
POST /api/v1/internal/jobs/{job} (external scheduler, e.g. GitHub Actions on a free
deploy) or by `scripts/run_job.py` (a platform cron, e.g. the Render cron in render.yaml).

Same service functions as app/worker.py; every job is idempotent, so a repeated or
overlapping run is safe (the cutoff also takes a per-date Redis lock)."""
import datetime as dt
import logging
from typing import Literal, get_args

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import batches as batch_svc
from app.services.cutoff import run_cutoff
from app.services.loyalty import sweep_unrewarded_referrals
from app.services.maintenance import prune
from app.services.ml import churn as churn_svc
from app.services.notifications import process_outbox
from app.services.subscription import today_ist

log = logging.getLogger("drs.jobs")

JobName = Literal["cutoff", "outbox", "housekeeping", "churn", "expire_batches"]
JOB_NAMES: tuple[str, ...] = get_args(JobName)


class JobInputError(ValueError):
    """Bad job input (e.g. a cutoff date that isn't in the future)."""


async def run_job(db: AsyncSession, job: str, date: dt.date | None = None) -> tuple[bool, dict]:
    """Run one job now. Returns (ok, result); ok is False when it ran but did not fully
    succeed (a 'partial' cutoff), so the scheduler can alert and re-run."""
    if job not in JOB_NAMES:
        raise JobInputError(f"unknown job {job!r} (one of: {', '.join(JOB_NAMES)})")
    if date is not None and job != "cutoff":
        raise JobInputError("date applies to the cutoff only")
    ok = True
    if job == "cutoff":
        delivery_date = date or (today_ist() + dt.timedelta(days=1))
        if delivery_date <= today_ist():
            # As on the admin endpoint without force: rebuilding today's orders mid-route,
            # or creating billable orders for a past date, is never a scheduled action.
            raise JobInputError("the cutoff can only run for tomorrow or later")
        result = await run_cutoff(db, delivery_date)
        result["delivery_date"] = str(result["delivery_date"])
        ok = not result.get("failed")  # 'locked' = a run for that date is in progress
    elif job == "outbox":
        # Per-message failures are retried / dead-lettered by the outbox itself.
        result = await process_outbox(db, limit=200)
    elif job == "housekeeping":
        result = await prune(db)
        result["referrals_rewarded"] = await sweep_unrewarded_referrals(db)
    elif job == "churn":
        result = await churn_svc.score_all(db)
    else:  # expire_batches, as of the IST business day
        result = await batch_svc.expire_batches(db, today_ist(), created_by=None)
        await db.commit()
    log.info("job %s ran: ok=%s %s", job, ok, result)
    return ok, result
