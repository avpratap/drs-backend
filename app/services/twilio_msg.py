"""Twilio delivery over SMS and WhatsApp (plain REST API, no SDK dependency).

Used for OTP delivery (app/services/otp_delivery.py) AND, when Twilio is configured, for
transactional notifications (app/services/notifications/senders.py). We generate/verify OTPs
ourselves (Redis); Twilio only delivers. On a Twilio trial, delivery only reaches VERIFIED
numbers; for India production SMS, DLT registration is still required.
"""
import logging

import httpx

from app.core.config import settings

log = logging.getLogger("drs.twilio")

_TWILIO_API = "https://api.twilio.com/2010-04-01"


def _sid() -> str:
    return settings.TWILIO_ACCOUNT_SID.strip()


def _token() -> str:
    return settings.TWILIO_AUTH_TOKEN.strip()


def twilio_configured() -> bool:
    return bool(_sid() and _token())


def sms_sender() -> str:
    return settings.TWILIO_SMS_FROM.strip()


def whatsapp_sender() -> str:
    return settings.TWILIO_WHATSAPP_FROM.strip()


def to_e164_india(phone: str) -> str:
    """Best-effort E.164 for Indian mobiles: 10 digits -> +91XXXXXXXXXX; keep an existing CC."""
    m = phone.strip().lstrip("+")
    if m.startswith("91") and len(m) > 10:
        return "+" + m
    if len(m) == 10:
        return "+91" + m
    return "+" + m


def _wa(addr: str) -> str:
    a = addr.strip()
    if a.startswith("whatsapp:"):
        return a
    return "whatsapp:" + (a if a.startswith("+") else "+" + a.lstrip("+"))


async def _message(from_: str, to: str, body: str) -> tuple[bool, str | None, str | None]:
    """POST one Twilio message. Returns (ok, message_sid, error). Credentials are stripped so
    a stray space/newline from an env paste can't cause a 401. Failures are logged (no secrets,
    no PII) so delivery problems are diagnosable in the service logs."""
    url = f"{_TWILIO_API}/Accounts/{_sid()}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                data={"From": from_, "To": to, "Body": body},
                auth=(_sid(), _token()),
            )
        if resp.status_code in (200, 201):  # Twilio returns 201 Created
            j = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            return True, j.get("sid"), None
        err = f"twilio {resp.status_code}: {resp.text[:200]}"
        log.warning("twilio send failed: %s", err)  # text is Twilio's error, not our creds
        return False, None, err
    except Exception as exc:
        log.warning("twilio send error: %s", exc)
        return False, None, f"twilio error: {exc}"


# ---------- Generic senders (used by notifications): return (ok, sid, err) ----------
async def send_sms(to_phone: str, body: str) -> tuple[bool, str | None, str | None]:
    if not (twilio_configured() and sms_sender()):
        return False, None, "twilio sms not configured"
    return await _message(sms_sender(), to_e164_india(to_phone), body)


async def send_whatsapp(to_phone: str, body: str) -> tuple[bool, str | None, str | None]:
    if not (twilio_configured() and whatsapp_sender()):
        return False, None, "twilio whatsapp not configured"
    return await _message(_wa(whatsapp_sender()), _wa(to_e164_india(to_phone)), body)


# ---------- OTP wrappers (return bool), used by otp_delivery ----------
def _otp_body(otp: str, requested_by: str | None) -> str:
    mins = settings.MSG91_OTP_EXPIRY_MIN
    if requested_by:  # relay mode: tell the owner which number asked
        return f"DRS login OTP for {requested_by}: {otp} (valid {mins} min)"
    return f"{otp} is your DRS verification code. Valid {mins} min. Do not share it."


async def send_otp_sms(to_phone: str, otp: str, requested_by: str | None = None) -> bool:
    ok, _, _ = await send_sms(to_phone, _otp_body(otp, requested_by))
    return ok


async def send_otp_whatsapp(to_phone: str, otp: str, requested_by: str | None = None) -> bool:
    ok, _, _ = await send_whatsapp(to_phone, _otp_body(otp, requested_by))
    return ok
