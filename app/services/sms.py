"""MSG91 OTP SMS.

We generate/verify the OTP ourselves (Redis) and use MSG91 only to *deliver* it,
so the provider stays swappable. Requires a DLT-approved template_id to actually send.
"""
import httpx

from app.core.config import settings

MSG91_OTP_URL = "https://control.msg91.com/api/v5/otp"


def sms_enabled() -> bool:
    return bool(settings.MSG91_AUTHKEY and settings.MSG91_TEMPLATE_ID)


def _to_msg91_mobile(phone: str) -> str:
    m = phone.lstrip("+")
    return m if m.startswith("91") and len(m) > 10 else ("91" + m if len(m) == 10 else m)


async def send_otp_sms(phone: str, otp: str) -> bool:
    """Deliver our OTP via MSG91. Returns True if MSG91 accepted it."""
    if not sms_enabled():
        return False
    params = {
        "template_id": settings.MSG91_TEMPLATE_ID,
        "mobile": _to_msg91_mobile(phone),
        "otp": otp,
        "otp_expiry": settings.MSG91_OTP_EXPIRY_MIN,
    }
    if settings.MSG91_SENDER:
        params["sender"] = settings.MSG91_SENDER
    headers = {"authkey": settings.MSG91_AUTHKEY, "accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(MSG91_OTP_URL, params=params, headers=headers)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return resp.status_code == 200 and str(data.get("type", "")).lower() == "success"
    except Exception:
        return False
