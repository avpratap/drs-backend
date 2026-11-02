"""Delivers an already-generated OTP to the user, across providers/channels.

Order: Twilio (if configured) over the OTP_CHANNEL, then MSG91 SMS. Returns True if any
provider accepted it. The Redis OTP engine is unchanged; this only handles delivery.

OTP_RELAY_TO (testing): when set, every code goes to that one number instead of the
requesting phone, labelled with who requested it — so codes can be relayed on a Twilio trial
(which only reaches verified numbers).
"""
from app.core.config import settings
from app.services import twilio_msg
from app.services.sms import send_otp_sms as msg91_send_otp_sms
from app.services.sms import sms_enabled as msg91_enabled


async def deliver_otp(phone: str, otp: str) -> bool:
    relay_to = settings.OTP_RELAY_TO.strip()
    target = relay_to or phone
    requested_by = phone if relay_to else None

    if twilio_msg.twilio_configured():
        channel = (settings.OTP_CHANNEL or "sms").strip().lower()
        if channel == "whatsapp":
            if await twilio_msg.send_otp_whatsapp(target, otp, requested_by=requested_by):
                return True
        elif await twilio_msg.send_otp_sms(target, otp, requested_by=requested_by):
            return True

    # MSG91 fallback (delivers to the requesting phone; no relay/label support).
    if msg91_enabled():
        return await msg91_send_otp_sms(phone, otp)
    return False
