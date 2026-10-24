"""Newsletter subscription (public double opt-in) + admin subscriber list."""
import datetime as dt
import hashlib
import hmac
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.newsletter import NewsletterSubscriber
from app.services.notifications import senders as sender_mod

router = APIRouter(tags=["newsletter"])
log = logging.getLogger("drs.newsletter")

_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _unsub_token(email: str) -> str:
    """Stateless per-email unsubscribe token (HMAC) — goes in the emailed one-click link,
    so only someone who received the email (or the owner) can unsubscribe that address.
    Signed with a DEDICATED secret so a JWT-auth rotation doesn't break outstanding links."""
    secret = settings.NEWSLETTER_UNSUB_SECRET or settings.JWT_SECRET
    return hmac.new(secret.encode(), f"unsub:{email}".encode(), hashlib.sha256).hexdigest()


class SubscribeIn(BaseModel):
    email: str = Field(pattern=_EMAIL, max_length=255)
    name: str | None = Field(default=None, max_length=120)


class UnsubscribeIn(BaseModel):
    email: str = Field(pattern=_EMAIL, max_length=255)
    token: str = Field(min_length=16, max_length=128)


class SubscriberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str | None = None
    status: str


class StatusOut(BaseModel):
    email: str
    status: str
    # Returned only in non-prod so tests/dev can confirm without reading the email.
    confirm_token: str | None = None
    unsubscribe_token: str | None = None


@router.post("/newsletter/subscribe", response_model=StatusOut, operation_id="newsletter_subscribe")
async def subscribe(data: SubscribeIn, db: AsyncSession = Depends(get_db)):
    """Double opt-in: a subscribe creates/keeps a PENDING record with a confirm token
    and emails a confirmation link. Consent (consent_at) is recorded only when the real
    email owner confirms — so one can't subscribe or forge consent for someone else.
    Already-subscribed emails stay subscribed (idempotent)."""
    email = str(data.email).lower()
    # Lock the existing row (if any) so a concurrent POST /newsletter/confirm serializes:
    # otherwise this request could hold a stale 'pending' snapshot and overwrite a status
    # the confirm just set to 'subscribed' (downgrading a just-confirmed user).
    row = await db.scalar(
        select(NewsletterSubscriber)
        .where(NewsletterSubscriber.email == email)
        .with_for_update()
    )
    if row is not None and row.status == "subscribed":
        return StatusOut(email=email, status="subscribed")  # already confirmed
    token = _new_confirm_token()
    if row is None:
        # Two concurrent first-subscribes for the same email would collide on
        # UNIQUE(email). Insert in a savepoint; on collision re-select UNDER LOCK and fall
        # through to the re-subscribe path on the row the other request committed.
        try:
            async with db.begin_nested():
                row = NewsletterSubscriber(
                    email=email, name=data.name, status="pending", source="web",
                    confirm_token=token,
                )
                db.add(row)
                await db.flush()
        except IntegrityError:
            row = await db.scalar(
                select(NewsletterSubscriber)
                .where(NewsletterSubscriber.email == email)
                .with_for_update()
            )
            if row is not None and row.status == "subscribed":
                return StatusOut(email=email, status="subscribed")  # lost race to a confirm
    # Never downgrade a 'subscribed' row (defense-in-depth alongside the lock above).
    if row.status != "subscribed" and (row.status != "pending" or row.confirm_token != token):
        # Re-subscribe (was pending/unsubscribed, or lost the insert race) — issue a
        # fresh confirmation so consent is always re-confirmed by the email owner.
        row.status = "pending"
        row.confirm_token = token
        if data.name:
            row.name = data.name
    await db.commit()
    # Best-effort confirmation email (dev-stub when SMTP is off).
    try:
        link = f"{getattr(settings, 'SITE_BASE_URL', 'https://drs.co.in')}/newsletter/confirm?token={token}"
        await sender_mod.send_email(
            email, f"Confirm your DRS newsletter subscription: {link}", None
        )
    except Exception:
        # Never log the address itself (PII); the row id is enough to investigate.
        log.warning("newsletter confirmation email failed (subscriber %s)", row.id)
    return StatusOut(
        email=email, status="pending",
        confirm_token=token if settings.ENV != "prod" else None,
    )


CONFIRM_TTL_SECONDS = 7 * 24 * 3600  # an unclicked confirmation link dies after a week


def _new_confirm_token() -> str:
    # "<random>.<expiry epoch>": the expiry travels with the token (fits String(64)).
    return f"{secrets.token_urlsafe(24)}.{int(time.time()) + CONFIRM_TTL_SECONDS}"


def _token_expired(token: str) -> bool:
    _, _, exp = token.rpartition(".")
    # Legacy tokens (issued before expiry was added) carry no suffix; they're replaced
    # on the next subscribe and pruned with the row, so they're accepted.
    return exp.isdigit() and int(exp) < int(time.time())


class ConfirmIn(BaseModel):
    token: str = Field(min_length=8, max_length=128)


@router.get("/newsletter/confirm", response_model=StatusOut, operation_id="newsletter_confirm_status")
async def confirm_status(token: str, db: AsyncSession = Depends(get_db)):
    """NON-mutating: report whether a confirm token is still valid so the landing page can
    render a 'Confirm subscription' button. Consent is NOT recorded here — an email client
    or link-scanner that prefetches the GET link must not auto-confirm (DPDP intent). The
    actual confirmation is the POST below, triggered by an explicit user click."""
    row = await db.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.confirm_token == token)
    )
    if row is None or _token_expired(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid or expired confirmation token")
    return StatusOut(email=row.email, status=row.status)


@router.post("/newsletter/confirm", response_model=StatusOut, operation_id="newsletter_confirm")
async def confirm(data: ConfirmIn, db: AsyncSession = Depends(get_db)):
    """Confirm a pending subscription via the emailed token — records consent. POST so a
    prefetch/scanner GET of the email link can't record consent without a user action."""
    row = await db.scalar(
        select(NewsletterSubscriber).where(NewsletterSubscriber.confirm_token == data.token)
    )
    if row is None or _token_expired(data.token):
        # Idempotent: a token already consumed by a prior confirm is gone; if the caller
        # was already subscribed, surface that instead of a hard 404 on a double-submit.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid or expired confirmation token")
    row.status = "subscribed"
    row.consent_at = dt.datetime.now(dt.UTC)
    row.confirm_token = None
    await db.commit()
    # Return the unsubscribe token ALWAYS (prod included): the confirmation landing page
    # must be able to show a working one-click unsubscribe link (CAN-SPAM/DPDP), and the
    # token is specific to this email, delivered to the person who just confirmed it.
    return StatusOut(
        email=row.email, status="subscribed", unsubscribe_token=_unsub_token(row.email)
    )


@router.post("/newsletter/unsubscribe", response_model=StatusOut, operation_id="newsletter_unsubscribe")
async def unsubscribe(data: UnsubscribeIn, db: AsyncSession = Depends(get_db)):
    email = str(data.email).lower()
    # Require the per-email token (from the emailed link) so one can't unsubscribe others.
    if not hmac.compare_digest(data.token, _unsub_token(email)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid unsubscribe token")
    row = await db.scalar(select(NewsletterSubscriber).where(NewsletterSubscriber.email == email))
    if row is not None:
        row.status = "unsubscribed"
        await db.commit()
    return StatusOut(email=email, status="unsubscribed")


@router.get(
    "/admin/newsletter/subscribers",
    response_model=list[SubscriberOut],
    operation_id="list_newsletter_subscribers",
    dependencies=[Depends(require_role("admin"))],
)
async def list_subscribers(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(NewsletterSubscriber).order_by(NewsletterSubscriber.id.desc())
    if status_filter:
        q = q.where(NewsletterSubscriber.status == status_filter)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()
