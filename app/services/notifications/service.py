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
from app.models.device_token import DeviceToken
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
    "push": "marketing_push",
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
    e = min(max(int(prof.quiet_hours_end or 0), 0), 23)  # clamp to a valid hour
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


def _gate_reason(prof, consents: dict, channel: str, category: str) -> str | None:
    """Why this channel may NOT receive this category now (None = allowed). Shared by
    emit and by delivery, so a consent withdrawn after queueing is honoured at send."""
    if not getattr(prof, _TOGGLE_ATTR[channel], False):
        return "channel_off"
    if category == tmpl.MARKETING:
        if not prof.marketing_opt_in:
            return "no_marketing_optin"
        purpose = _CONSENT_PURPOSE.get(channel)
        # Fail closed: a marketing channel with no consent purpose is never sent.
        if purpose is None or consents.get(purpose) != "granted":
            return "no_consent"
    return None


async def _active_push_tokens(db: AsyncSession, user_id: int) -> str | None:
    tokens = (
        await db.execute(
            select(DeviceToken.token).where(
                DeviceToken.user_id == user_id, DeviceToken.active.is_(True)
            )
        )
    ).scalars().all()
    return ",".join(tokens) if tokens else None


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
    try:
        # The WHOLE emit runs in a savepoint (the per-row ones nest inside): a failure in
        # e.g. the profile get-or-create flush (concurrent first event for a new user) or a
        # consent/device query used to leave the session in a failed transaction, so the
        # CALLER's commit (payment / billing) then raised and its money write was lost.
        async with db.begin_nested():
            return await _emit_event_inner(db, event_type, user, data, spec, account_id, dedupe_key)
    except Exception:
        # Notifications are best-effort relative to the caller's (often money-critical)
        # transaction: a failure here must NEVER roll back the caller. Each row insert
        # already uses its own savepoint; this outer guard catches anything else
        # (profile/consent/device-token query errors, bad data) so emit can't propagate.
        log.exception("emit_event failed for %s", event_type)
        return []


async def _emit_event_inner(
    db: AsyncSession,
    event_type: str,
    user: User,
    data: dict,
    spec,
    account_id: int | None,
    dedupe_key: str | None,
) -> list[Notification]:
    category, channels, render = spec
    rendered = render(data)
    prof = await profile_svc.get_or_create_profile(db, user)
    consents = (
        await profile_svc.current_consents(db, user.id) if category == tmpl.MARKETING else {}
    )
    now = _now_ist()

    rows: list[Notification] = []
    for channel in channels:
        status, skip_reason, scheduled_for = "queued", None, None
        reason = _gate_reason(prof, consents, channel, category)
        if reason is not None:
            status, skip_reason = "skipped", reason
        elif category == tmpl.MARKETING and _in_quiet_hours(prof, now):
            scheduled_for = _quiet_end(prof, now)

        to_addr = _addr(channel, user)
        if channel == "push":
            # Push "address" is the user's active device tokens (re-resolved at delivery).
            to_addr = await _active_push_tokens(db, user.id)
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
    """Send one queued notification, committing its own state. Returns True on success.

    Two-phase to avoid a provider DOUBLE-SEND (#N4): the row is durably claimed as
    'sending' BEFORE the provider call, then set to 'sent'/'queued'/'failed' after. A
    crash between those two commits leaves the row 'sending' — it is NOT auto-retried
    (the candidate scan only picks 'queued'), so a paid WhatsApp/SMS can't be re-sent
    behind our back. A stuck 'sending' row is recovered via admin resend (resets to
    queued). This trades at-least-once for at-most-once, which is correct for paid sends.
    """
    if notif.status != "queued":
        return notif.status == "sent"
    # Re-check gating AT SEND TIME: a row may have waited (quiet hours, retry backoff,
    # admin resend) while the user withdrew consent / turned the channel off / was erased.
    user = await db.get(User, notif.user_id)
    if user is None or user.status == "erased":
        notif.status, notif.skip_reason = "skipped", "user_erased"
        await db.commit()
        return False
    if notif.category == tmpl.MARKETING or notif.channel in _TOGGLE_ATTR:
        prof = await profile_svc.get_or_create_profile(db, user)
        consents = (
            await profile_svc.current_consents(db, user.id)
            if notif.category == tmpl.MARKETING else {}
        )
        reason = _gate_reason(prof, consents, notif.channel, notif.category)
        if reason is not None:
            notif.status = "skipped"
            notif.skip_reason = "consent_withdrawn" if reason == "no_consent" else reason
            await db.commit()
            return False
    if notif.channel == "push":
        # Tokens deactivated since emit (logout / reassigned) must not receive it.
        notif.to_addr = await _active_push_tokens(db, notif.user_id)
        if not notif.to_addr:
            notif.status, notif.skip_reason = "skipped", "no_address"
            await db.commit()
            return False
    sender = sender_mod.SENDERS.get(notif.channel)
    if sender is None:
        notif.status, notif.error = "failed", "no sender"
        await db.commit()
        return False
    # Phase 1 — claim durably so a crash mid-send can't resurface this as 'queued'.
    notif.status = "sending"
    notif.attempts = (notif.attempts or 0) + 1
    notif.last_attempt_at = dt.datetime.now(dt.UTC)
    await db.commit()
    # Phase 2 — call the provider, then record the terminal state.
    ok, msg_id, err = await sender(notif.to_addr or "", notif.body or "", notif.payload)
    if ok:
        notif.status = "sent"
        notif.provider = settings.WHATSAPP_PROVIDER if notif.channel == "whatsapp" else notif.channel
        notif.provider_message_id = msg_id
        notif.sent_at = dt.datetime.now(dt.UTC)
        notif.next_attempt_at = None
        notif.error = None
    else:
        notif.error = err
        if notif.attempts >= MAX_ATTEMPTS:
            notif.status = "failed"
        else:
            # Back to queued with exponential backoff: retry no sooner than 2**attempts min.
            notif.status = "queued"
            notif.next_attempt_at = notif.last_attempt_at + dt.timedelta(minutes=2**notif.attempts)
    await db.commit()
    return ok


async def process_outbox(db: AsyncSession, *, limit: int = 100) -> dict:
    """Drain due queued notifications (worker poller + admin flush endpoint)."""
    now = dt.datetime.now(dt.UTC)
    # Pick candidate ids WITHOUT holding locks across the whole drain (a batch-wide
    # FOR UPDATE would be released by the first per-row commit, opening a double-send
    # window for an overlapping drain). Each row is then re-locked individually below.
    ids = (
        await db.execute(
            select(Notification.id)
            .where(
                Notification.status == "queued",
                or_(Notification.scheduled_for.is_(None), Notification.scheduled_for <= now),
                or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= now),
            )
            .order_by(Notification.id)
            .limit(limit)
        )
    ).scalars().all()
    sent = failed = processed = 0
    for nid in ids:
        # Re-lock this one row for the duration of its own send+commit. skip_locked means
        # a concurrent drain won't also grab it; the status re-check catches a row another
        # worker already sent between our candidate scan and this lock.
        notif = (
            await db.execute(
                select(Notification)
                .where(Notification.id == nid, Notification.status == "queued")
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if notif is None:
            continue
        processed += 1
        try:
            # deliver_notification commits its own claim + terminal state (two-phase).
            ok = await deliver_notification(db, notif)
            sent += int(ok)
            failed += int(not ok)
        except Exception:
            await db.rollback()
            failed += 1
            log.exception("deliver_notification crashed for notification %s", nid)
            # Best-effort: only a row still 'queued' (crash BEFORE the provider call) is
            # marked failed. A 'sending' row (crash AFTER the call) is left as-is — the
            # provider may have delivered it, so we must not auto-retry; admin resend
            # recovers it deliberately.
            try:
                n = await db.get(Notification, nid)
                if n is not None and n.status == "queued":
                    n.status, n.error = "failed", "delivery crashed"
                    await db.commit()
                else:
                    await db.rollback()
            except Exception:
                await db.rollback()
    return {"processed": processed, "sent": sent, "failed": failed}
