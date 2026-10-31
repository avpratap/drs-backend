"""Scheduled-job trigger for deploys WITHOUT a background worker (e.g. Render free tier).

The ARQ worker (app/worker.py) runs these on its own cron when a worker is deployed.
Without one, an external scheduler (.github/workflows/scheduled-jobs.yml) calls
POST /api/v1/internal/jobs/{job}; the job runs INLINE in the request (app.services.jobs).

Auth is a dedicated shared secret, JOBS_TOKEN (``Authorization: Bearer <JOBS_TOKEN>``),
not an admin login: admin access tokens live 15 minutes and refresh tokens rotate on use,
so neither can live in a CI secret. With JOBS_TOKEN unset the endpoint is disabled (404);
in prod the boot guard requires it to be >= 32 chars when set.
"""
import datetime as dt
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.services import jobs as jobs_svc

router = APIRouter(prefix="/internal/jobs", tags=["jobs"])


class JobRunOut(BaseModel):
    job: str
    # False when the job ran but did not fully succeed (e.g. a 'partial' cutoff): the
    # scheduler should alert. Every job is idempotent, so simply re-run it.
    ok: bool
    result: dict


def require_jobs_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.JOBS_TOKEN
    if not expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")  # disabled
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.strip().encode(), expected.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid jobs token")


@router.post(
    "/{job}", response_model=JobRunOut, operation_id="run_scheduled_job",
    dependencies=[Depends(require_jobs_token)],
    responses={401: {"description": "bad or missing jobs token"},
               404: {"description": "jobs endpoint disabled (JOBS_TOKEN unset)"}},
)
async def run_scheduled_job(
    job: jobs_svc.JobName, date: dt.date | None = None, db: AsyncSession = Depends(get_db),
):
    """Run one scheduled job now, inline.

    - cutoff: materialize standing orders into delivery_orders for `date` (default
      tomorrow, IST); today/past dates are refused.
    - outbox: deliver due queued notifications (WhatsApp/SMS/push/email).
    - housekeeping: prune short-retention rows + reward missed referrals.
    - churn: churn scoring over all accounts.
    - expire_batches: write off stock batches past expiry (as of today, IST)."""
    try:
        ok, result = await jobs_svc.run_job(db, job, date)
    except jobs_svc.JobInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return JobRunOut(job=job, ok=ok, result=result)
