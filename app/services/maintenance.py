"""Retention / housekeeping: prune rows that only need short-term retention so
idempotency_keys, webhook_events and notifications don't grow unbounded."""
import datetime as dt
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import IdempotencyKey
from app.models.notification import Notification
from app.models.refund import WebhookEvent

IDEMPOTENCY_TTL_HOURS = 48
# A claim still "in progress" (response_status NULL) this long after creation is an
# orphan from a hard crash (SIGKILL between claim-commit and response-store). The
# middleware never re-executes such a claim in-request (that could double-charge); the
# reaper clears it out-of-band so a genuine retry can proceed. Generous vs any real
# handler runtime (AI ≤~93s, gateway ≤~40s).
IDEMPOTENCY_INPROGRESS_TTL_MIN = 15
WEBHOOK_TTL_DAYS = 30
NOTIFICATION_DONE_TTL_DAYS = 30
NOTIFICATION_FAILED_TTL_DAYS = 90

log = logging.getLogger("drs.maintenance")


async def prune(db: AsyncSession) -> dict:
    now = dt.datetime.now(dt.UTC)
    counts: dict[str, int] = {}

    r = await db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.created_at < now - dt.timedelta(hours=IDEMPOTENCY_TTL_HOURS)
        )
    )
    counts["idempotency_keys"] = r.rowcount or 0

    # Reap orphaned in-progress claims so a crashed write isn't wedged on 409 until the
    # 48h prune (the middleware no longer auto-reclaims these in-request).
    r = await db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.response_status.is_(None),
            IdempotencyKey.created_at
            < now - dt.timedelta(minutes=IDEMPOTENCY_INPROGRESS_TTL_MIN),
        )
    )
    counts["idempotency_inprogress_reaped"] = r.rowcount or 0

    r = await db.execute(
        delete(WebhookEvent).where(
            WebhookEvent.created_at < now - dt.timedelta(days=WEBHOOK_TTL_DAYS)
        )
    )
    counts["webhook_events"] = r.rowcount or 0

    r = await db.execute(
        delete(Notification).where(
            Notification.status.in_(("sent", "skipped")),
            Notification.created_at < now - dt.timedelta(days=NOTIFICATION_DONE_TTL_DAYS),
        )
    )
    counts["notifications_done"] = r.rowcount or 0

    # Surface rows stuck in 'sending' (crash between the provider call and the result
    # commit). They are deliberately NOT auto-retried (at-most-once for paid sends), so
    # make them visible for an operator to check + resend.
    stuck = await db.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.status == "sending",
            Notification.last_attempt_at < now - dt.timedelta(minutes=15),
        )
    )
    counts["notifications_stuck_sending"] = int(stuck or 0)
    if stuck:
        log.error("%s notification(s) stuck in 'sending' >15min — check provider + resend", stuck)

    # Keep failed (DLQ) rows longer for inspection, but not forever.
    r = await db.execute(
        delete(Notification).where(
            Notification.status == "failed",
            Notification.created_at < now - dt.timedelta(days=NOTIFICATION_FAILED_TTL_DAYS),
        )
    )
    counts["notifications_failed"] = r.rowcount or 0

    await db.commit()
    return counts
