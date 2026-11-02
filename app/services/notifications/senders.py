"""Channel senders. Each returns (ok: bool, provider_message_id: str|None, error: str|None).

No credentials configured -> dev stub: the message is logged and treated as sent
with a synthetic id, so the whole pipeline is testable without external accounts.
"""
import json
import logging
import secrets

import httpx

from app.core.config import settings
from app.services import twilio_msg
from app.services.sms import _to_msg91_mobile, sms_enabled

log = logging.getLogger("drs.notify")

GUPSHUP_URL = "https://api.gupshup.io/wa/api/v1/msg"


def whatsapp_enabled() -> bool:
    return bool(settings.GUPSHUP_API_KEY and settings.GUPSHUP_SOURCE)


def _mask(addr: str | None) -> str:
    return f"***{addr[-4:]}" if addr and len(addr) > 4 else "***"


def _stub(channel: str, to_addr: str, body: str) -> tuple[bool, str | None, str | None]:
    # Never log the recipient or the message body (PII: phone/email, order details).
    if settings.ENV == "prod":
        # An unconfigured channel in prod must FAIL (visible, retried/alerted) — not
        # pretend the customer was notified.
        log.error("[notify:%s] channel not configured in prod; to=%s", channel, _mask(to_addr))
        return False, None, f"{channel} channel not configured"
    log.info("[notify:%s:STUB] to=%s len=%d", channel, _mask(to_addr), len(body or ""))
    return True, f"stub_{channel}_{secrets.token_hex(6)}", None


def twilio_sms_ready() -> bool:
    return twilio_msg.twilio_configured() and bool(settings.TWILIO_SMS_FROM.strip())


def twilio_whatsapp_ready() -> bool:
    return twilio_msg.twilio_configured() and bool(settings.TWILIO_WHATSAPP_FROM.strip())


def channel_unavailable(channel: str) -> str | None:
    """A channel that can't deliver in this deployment (skip, don't retry 5x into the
    DLQ). Transactional SMS needs a provider: Twilio (if configured) can deliver it;
    otherwise MSG91 needs a DLT-approved flow that isn't wired yet, so in prod SMS is
    skipped. Other unconfigured channels still FAIL loudly in prod (_stub)."""
    if channel == "sms" and settings.ENV == "prod" and not twilio_sms_ready():
        return "provider_unavailable"
    return None


async def send_whatsapp(to_addr: str, body: str, payload: dict | None) -> tuple[bool, str | None, str | None]:
    # Prefer Twilio when configured (same provider as OTP); else Gupshup BSP; else dev stub.
    if twilio_whatsapp_ready():
        return await twilio_msg.send_whatsapp(to_addr, body)
    if not whatsapp_enabled():
        return _stub("whatsapp", to_addr, body)
    # Gupshup session message (text). Template sends use the template API once
    # wa_template names are approved; we send text here as the baseline.
    data = {
        "channel": "whatsapp",
        "source": settings.GUPSHUP_SOURCE,
        "destination": _to_msg91_mobile(to_addr),
        "src.name": settings.GUPSHUP_SRC_NAME or settings.GUPSHUP_APP_NAME,
        # Proper JSON (json.dumps) — Python repr produced single quotes / invalid JSON and
        # broke on bodies containing quotes or newlines.
        "message": json.dumps({"type": "text", "text": body}),
    }
    headers = {"apikey": settings.GUPSHUP_API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(GUPSHUP_URL, data=data, headers=headers)
        j = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code < 300 and str(j.get("status", "")).lower() in ("submitted", "success"):
            return True, j.get("messageId"), None
        return False, None, f"gupshup {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, None, f"gupshup error: {exc}"


async def send_sms(to_addr: str, body: str, payload: dict | None) -> tuple[bool, str | None, str | None]:
    # Prefer Twilio when configured (same provider as OTP). MSG91 transactional SMS needs a
    # DLT-approved flow/template that isn't wired yet, so without Twilio we dev-stub.
    if twilio_sms_ready():
        return await twilio_msg.send_sms(to_addr, body)
    if not sms_enabled():
        return _stub("sms", to_addr, body)
    # Placeholder for MSG91 flow API; stub-send for now to avoid half-wired calls.
    return _stub("sms", to_addr, body)


def fcm_enabled() -> bool:
    return bool(settings.FCM_PROJECT_ID and settings.FCM_CREDENTIALS_JSON)


def sendgrid_enabled() -> bool:
    return bool(settings.SENDGRID_API_KEY.strip())


def email_enabled() -> bool:
    # SendGrid (Twilio's email product) via its API, or any SMTP relay (incl. SendGrid SMTP).
    return bool(settings.SENDGRID_API_KEY.strip() or settings.SMTP_HOST.strip())


def _from_email() -> str:
    """Bare sender address for the SendGrid API (SMTP_FROM may be 'DRS <a@b>')."""
    import re

    frm = settings.SENDGRID_FROM.strip() or settings.SMTP_FROM
    m = re.search(r"<([^>]+)>", frm)
    return (m.group(1) if m else frm).strip()


async def _send_email_sendgrid(
    to_addr: str, body: str, payload: dict | None
) -> tuple[bool, str | None, str | None]:
    data = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": _from_email()},
        "subject": (payload or {}).get("subject", "DRS notification"),
        "content": [{"type": "text/plain", "value": body}],
    }
    headers = {
        "Authorization": f"Bearer {settings.SENDGRID_API_KEY.strip()}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post("https://api.sendgrid.com/v3/mail/send", json=data, headers=headers)
        if resp.status_code in (200, 202):  # SendGrid returns 202 Accepted
            return True, resp.headers.get("X-Message-Id"), None
        return False, None, f"sendgrid {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, None, f"sendgrid error: {exc}"


def _fcm_access_token() -> str:
    """OAuth2 access token from the service-account JSON (lazy google-auth)."""
    import json

    from google.oauth2 import service_account  # lazy

    raw = settings.FCM_CREDENTIALS_JSON
    if raw.strip().startswith("{"):
        info = json.loads(raw)
    else:
        with open(raw) as fh:  # path to the service-account file — close the fd
            info = json.load(fh)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/firebase.messaging"]
    )
    from google.auth.transport.requests import Request as GRequest  # lazy

    creds.refresh(GRequest())
    return creds.token


# Keys copied from the event payload into the FCM `data` block so a tap deep-links to
# the right screen (the app routes on `type` + these ids). An allow-list: amounts,
# balances and free text stay out of the device-visible data. FCM data values must be
# strings.
PUSH_DATA_KEYS = (
    "type", "order_id", "delivery_id", "shipment_id", "subscription_id", "payment_id",
    "shop_id", "date",
)


def push_data(payload: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in PUSH_DATA_KEYS:
        value = (payload or {}).get(key)
        if value is not None and isinstance(value, (str, int, float)) and not isinstance(value, bool):
            out[key] = str(value)
    return out


async def send_push(to_addr: str, body: str, payload: dict | None) -> tuple[bool, str | None, str | None]:
    # to_addr is a comma-joined list of the user's active device tokens (resolved at
    # emit time). No creds -> dev stub.
    tokens = [t for t in (to_addr or "").split(",") if t]
    if not fcm_enabled():
        return _stub("push", to_addr or "device", body)
    if not tokens:
        return False, None, "no device tokens"
    try:
        import asyncio

        token = await asyncio.to_thread(_fcm_access_token)  # blocking crypto -> thread
        url = f"https://fcm.googleapis.com/v1/projects/{settings.FCM_PROJECT_ID}/messages:send"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        sent_ids, last_err = [], None
        data = push_data(payload)
        async with httpx.AsyncClient(timeout=10) as client:
            for tok in tokens:
                msg = {"message": {"token": tok, "notification": {"title": "DRS", "body": body}}}
                if data:
                    msg["message"]["data"] = data
                resp = await client.post(url, json=msg, headers=headers)
                if resp.status_code < 300:
                    sent_ids.append(resp.json().get("name", tok[:12]))
                else:
                    last_err = f"fcm {resp.status_code}: {resp.text[:120]}"
        if sent_ids:
            return True, ",".join(sent_ids), last_err
        return False, None, last_err or "fcm: all tokens failed"
    except Exception as exc:
        return False, None, f"fcm error: {exc}"


async def send_email(to_addr: str, body: str, payload: dict | None) -> tuple[bool, str | None, str | None]:
    if not email_enabled():
        return _stub("email", to_addr or "email", body)
    if not to_addr:
        return False, None, "no email address"
    if sendgrid_enabled():  # SendGrid Web API (preferred); SMTP below is the fallback
        return await _send_email_sendgrid(to_addr, body, payload)
    try:
        import email.message

        import aiosmtplib  # lazy

        msg = email.message.EmailMessage()
        msg["From"] = settings.SMTP_FROM
        msg["To"] = to_addr
        msg["Subject"] = (payload or {}).get("subject", "DRS notification")
        msg.set_content(body)
        await aiosmtplib.send(
            msg, hostname=settings.SMTP_HOST, port=settings.SMTP_PORT,
            username=settings.SMTP_USER or None, password=settings.SMTP_PASSWORD or None,
            start_tls=settings.SMTP_PORT == 587,
        )
        return True, f"smtp_{secrets.token_hex(6)}", None
    except Exception as exc:
        return False, None, f"smtp error: {exc}"


SENDERS = {
    "whatsapp": send_whatsapp,
    "sms": send_sms,
    "push": send_push,
    "email": send_email,
}
