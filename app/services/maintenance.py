"""Retention / housekeeping: prune rows that only need short-term retention so
idempotency_keys, webhook_events and notifications don't grow unbounded."""
import datetime as dt

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import IdempotencyKey
from app.models.notification import Notification
from app.models.refund import WebhookEvent

IDEMPOTENCY_TTL_HOURS = 48
WEBHOOK_TTL_DAYS = 30
NOTIFICATION_DONE_TTL_DAYS = 30
NOTIFICATION_FAILED_TTL_DAYS = 90


async def prune(db: AsyncSession) -> dict:
    now = dt.datetime.now(dt.UTC)
    counts: dict[str, int] = {}

    r = await db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.created_at < now - dt.timedelta(hours=IDEMPOTENCY_TTL_HOURS)
        )
    )
    counts["idempotency_keys"] = r.rowcount or 0

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
