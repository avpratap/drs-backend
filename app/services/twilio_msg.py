"""Twilio OTP delivery over SMS and WhatsApp (plain REST API, no SDK dependency).

We still generate/verify the OTP ourselves (Redis); Twilio only *delivers* the code, so the
provider stays swappable alongside MSG91. On a Twilio trial, delivery only reaches VERIFIED
numbers; for India production SMS, DLT registration is still required.
"""
import httpx

from app.core.config import settings

_TWILIO_API = "https://api.twilio.com/2010-04-01"


def twilio_configured() -> bool:
    return bool(settings.TWILIO_ACCOUNT_SID.strip() and settings.TWILIO_AUTH_TOKEN.strip())


def to_e164_india(phone: str) -> str:
    """Best-effort E.164 for Indian mobiles: 10 digits -> +91XXXXXXXXXX; keep an existing CC."""
    m = phone.strip().lstrip("+")
    if m.startswith("91") and len(m) > 10:
        return "+" + m
    if len(m) == 10:
        return "+91" + m
    return "+" + m


def _body(otp: str, requested_by: str | None) -> str:
    mins = settings.MSG91_OTP_EXPIRY_MIN
    if requested_by:  # relay mode: tell the owner which number asked
        return f"DRS login OTP for {requested_by}: {otp} (valid {mins} min)"
    return f"{otp} is your DRS verification code. Valid {mins} min. Do not share it."


async def _send(from_: str, to: str, body: str) -> bool:
    url = f"{_TWILIO_API}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                data={"From": from_, "To": to, "Body": body},
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
            )
        # Twilio returns 201 Created for an accepted message.
        return resp.status_code in (200, 201)
    except Exception:
        return False


async def send_otp_sms(to_phone: str, otp: str, requested_by: str | None = None) -> bool:
    if not (twilio_configured() and settings.TWILIO_SMS_FROM.strip()):
        return False
    return await _send(settings.TWILIO_SMS_FROM.strip(), to_e164_india(to_phone), _body(otp, requested_by))


async def send_otp_whatsapp(to_phone: str, otp: str, requested_by: str | None = None) -> bool:
    if not (twilio_configured() and settings.TWILIO_WHATSAPP_FROM.strip()):
        return False
    frm = settings.TWILIO_WHATSAPP_FROM.strip()
    if not frm.startswith("whatsapp:"):
        frm = "whatsapp:" + (frm if frm.startswith("+") else "+" + frm.lstrip("+"))
    return await _send(frm, "whatsapp:" + to_e164_india(to_phone), _body(otp, requested_by))
