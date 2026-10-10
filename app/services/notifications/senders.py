"""Channel senders. Each returns (ok: bool, provider_message_id: str|None, error: str|None).

No credentials configured -> dev stub: the message is logged and treated as sent
with a synthetic id, so the whole pipeline is testable without external accounts.
"""
import logging
import secrets

import httpx

from app.core.config import settings
from app.services.sms import _to_msg91_mobile, sms_enabled

log = logging.getLogger("drs.notify")

GUPSHUP_URL = "https://api.gupshup.io/wa/api/v1/msg"


def whatsapp_enabled() -> bool:
    return bool(settings.GUPSHUP_API_KEY and settings.GUPSHUP_SOURCE)


def _stub(channel: str, to_addr: str, body: str) -> tuple[bool, str, None]:
    log.info("[notify:%s:STUB] to=%s body=%s", channel, to_addr, body)
    return True, f"stub_{channel}_{secrets.token_hex(6)}", None


async def send_whatsapp(to_addr: str, body: str, payload: dict | None) -> tuple[bool, str | None, str | None]:
    if not whatsapp_enabled():
        return _stub("whatsapp", to_addr, body)
    # Gupshup session message (text). Template sends use the template API once
    # wa_template names are approved; we send text here as the baseline.
    data = {
        "channel": "whatsapp",
        "source": settings.GUPSHUP_SOURCE,
        "destination": _to_msg91_mobile(to_addr),
        "src.name": settings.GUPSHUP_SRC_NAME or settings.GUPSHUP_APP_NAME,
        "message": f'{{"type":"text","text":{body!r}}}',
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
    # MSG91 transactional SMS requires a DLT-approved flow/template; until wired,
    # dev-stub. (OTP uses the dedicated MSG91 OTP endpoint; this is for alerts.)
    if not sms_enabled():
        return _stub("sms", to_addr, body)
    # Placeholder for MSG91 flow API; stub-send for now to avoid half-wired calls.
    return _stub("sms", to_addr, body)


async def send_push(to_addr: str, body: str, payload: dict | None) -> tuple[bool, str | None, str | None]:
    return _stub("push", to_addr or "device", body)


async def send_email(to_addr: str, body: str, payload: dict | None) -> tuple[bool, str | None, str | None]:
    return _stub("email", to_addr or "email", body)


SENDERS = {
    "whatsapp": send_whatsapp,
    "sms": send_sms,
    "push": send_push,
    "email": send_email,
}
