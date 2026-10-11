"""Notification orchestration: emit -> gate -> persist -> deliver.

Gating rules
------------
transactional: sent on any channel whose per-channel toggle is ON
  (notify_whatsapp/sms/push/email). Not gated by marketing consent; never
  deferred by quiet hours (these are service-critical).
marketing: requires channel toggle ON *and* marketing consent granted for that
  channel's purpose *and* marketing_opt_in. If inside quiet hours, the row is
  queued with scheduled_for = end of the quiet window (deferred, not dropped).
"""
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.notification import Notification
from app.models.user import User
from app.services import profile as profile_svc
from app.services.notifications import senders as sender_mod
from app.services.notifications import templates as tmpl

log = logging.getLogger("drs.notify")

MAX_ATTEMPTS = 5

_CONSENT_PURPOSE = {
    "whatsapp": "whatsapp_optin",
    "sms": "marketing_sms",
    "email": "marketing_email",
}
_TOGGLE_ATTR = {
    "whatsapp": "notify_whatsapp",
    "sms": "notify_sms",
    "push": "notify_push",
    "email": "notify_email",
}


def _now_ist() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(settings.TIMEZONE))


def _in_quiet_hours(prof, now: dt.datetime) -> bool:
    s, e = prof.quiet_hours_start, prof.quiet_hours_end
    if s is None or e is None or s == e:
        return False
    h = now.hour
    return s <= h < e if s < e else (h >= s or h < e)  # handle overnight wrap


def _quiet_end(prof, now: dt.datetime) -> dt.datetime:
    """Next datetime at which the quiet window ends."""
    e = prof.quiet_hours_end
    candidate = now.replace(hour=e, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate


def _addr(channel: str, user: User) -> str | None:
    if channel in ("whatsapp", "sms"):
        return user.phone
    if channel == "email":
        return user.email
    return None  # push -> device token (not modeled yet)


async def emit_event(
    db: AsyncSession,
    event_type: str,
    user: User,
    data: dict,
    *,
    account_id: int | None = None,
    dedupe_key: str | None = None,
) -> list[Notification]:
    """Create (gated) notification rows for an event. Flushes; caller commits."""
    spec = tmpl.get_spec(event_type)
    if spec is None:
        log.warning("emit_event: unknown event_type %s", event_type)
        return []
    category, channels, render = spec
    rendered = render(data)
    prof = await profile_svc.get_or_create_profile(db, user)
    consents = (
        await profile_svc.current_consents(db, user.id) if category == tmpl.MARKETING else {}
    )
    now = _now_ist()

    rows: list[Notification] = []
    for channel in channels:
        toggle_on = getattr(prof, _TOGGLE_ATTR[channel], False)
        status, skip_reason, scheduled_for = "queued", None, None

        if not toggle_on:
            status, skip_reason = "skipped", "channel_off"
        elif category == tmpl.MARKETING:
            if not prof.marketing_opt_in:
                status, skip_reason = "skipped", "no_marketing_optin"
            else:
                purpose = _CONSENT_PURPOSE.get(channel)
                if purpose and consents.get(purpose) != "granted":
                    status, skip_reason = "skipped", "no_consent"
                elif _in_quiet_hours(prof, now):
                    scheduled_for = _quiet_end(prof, now)

        to_addr = _addr(channel, user)
        if status == "queued" and not to_addr:
            status, skip_reason = "skipped", "no_address"

        row = Notification(
            user_id=user.id,
            account_id=account_id,
            event_type=event_type,
            channel=channel,
            category=category,
            template_code=rendered.get("wa_template"),
            to_addr=to_addr,
            body=rendered.get("text"),
            payload=data,
            status=status,
            skip_reason=skip_reason,
            scheduled_for=scheduled_for,
            dedupe_key=dedupe_key,
        )
        try:
            # Savepoint: a dedupe collision rolls back only this insert, never the
            # caller's outer transaction (cutoff / delivery billing).
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            continue  # duplicate (dedupe_key, channel) — already emitted
        rows.append(row)
    return rows


async def deliver_notification(db: AsyncSession, notif: Notification) -> bool:
    """Send one queued notification. Returns True on success."""
    if notif.status != "queued":
        return notif.status == "sent"
    sender = sender_mod.SENDERS.get(notif.channel)
    if sender is None:
        notif.status, notif.error = "failed", "no sender"
        return False
    notif.attempts += 1
    ok, msg_id, err = await sender(notif.to_addr or "", notif.body or "", notif.payload)
    if ok:
        notif.status = "sent"
        notif.provider = settings.WHATSAPP_PROVIDER if notif.channel == "whatsapp" else notif.channel
        notif.provider_message_id = msg_id
        notif.sent_at = dt.datetime.now(dt.UTC)
        notif.error = None
    else:
        notif.error = err
        if notif.attempts >= MAX_ATTEMPTS:
            notif.status = "failed"
    return ok


async def process_outbox(db: AsyncSession, *, limit: int = 100) -> dict:
    """Drain due queued notifications (worker poller + admin flush endpoint)."""
    now = dt.datetime.now(dt.UTC)
    res = await db.execute(
        select(Notification)
        .where(
            Notification.status == "queued",
            or_(Notification.scheduled_for.is_(None), Notification.scheduled_for <= now),
        )
        .order_by(Notification.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    batch = res.scalars().all()
    sent = failed = 0
    for notif in batch:
        ok = await deliver_notification(db, notif)
        sent += int(ok)
        failed += int(not ok)
    await db.commit()
    return {"processed": len(batch), "sent": sent, "failed": failed}
