"""Phone OTP via Redis.

Dev: the OTP is returned in the API response so you can test without MSG91.
Prod: wire send_sms() to MSG91 (DLT-registered) and never return the OTP.
"""
import hashlib
import secrets

from app.core.config import settings
from app.core.redis import redis_client
from app.services.sms import send_otp_sms, sms_enabled

# Floor at 60s so a misconfigured MSG91_OTP_EXPIRY_MIN=0 can't make every OTP expire
# instantly (redis SET ex=0 is rejected) and lock users out of login.
OTP_TTL = max(60, settings.MSG91_OTP_EXPIRY_MIN * 60)  # keep in sync with the SMS's stated expiry
MAX_SENDS_PER_HOUR = 5
MAX_SENDS_PER_IP_HOUR = 50  # across all phones from one device/IP
MAX_VERIFY_ATTEMPTS = 5
RESEND_COOLDOWN = 60  # seconds


def _hash_otp(phone: str, otp: str) -> str:
    return hashlib.sha256(f"{phone}:{otp}:{settings.JWT_SECRET}".encode()).hexdigest()


async def request_otp(phone: str, ip: str | None = None) -> str | None:
    """Generate + store an OTP. Returns the OTP in non-prod for testing, else None."""
    # Cooldown first so a client that's merely resending too fast doesn't burn its hourly
    # per-IP budget (the IP counter only increments once we're actually going to send).
    if await redis_client.get(f"otp:cooldown:{phone}"):
        raise ValueError("Please wait a moment before requesting another OTP")

    # Per-IP cap — stops a single device cycling through many phone numbers.
    if ip:
        ip_sends = await redis_client.incr(f"otp:ip:{ip}")
        if ip_sends == 1:
            await redis_client.expire(f"otp:ip:{ip}", 3600)
        if ip_sends > MAX_SENDS_PER_IP_HOUR:
            raise ValueError("Too many OTP requests from this device. Try again later.")

    sends = await redis_client.incr(f"otp:send:{phone}")
    if sends == 1:
        await redis_client.expire(f"otp:send:{phone}", 3600)
    if sends > MAX_SENDS_PER_HOUR:
        raise ValueError("Too many OTP requests. Try again later.")

    otp = f"{secrets.randbelow(10**6):06d}"
    await redis_client.set(f"otp:{phone}", _hash_otp(phone, otp), ex=OTP_TTL)
    await redis_client.set(f"otp:attempts:{phone}", 0, ex=OTP_TTL)
    await redis_client.set(f"otp:cooldown:{phone}", "1", ex=RESEND_COOLDOWN)

    sent = await send_otp_sms(phone, otp) if sms_enabled() else False
    # Return the OTP for testing ONLY when we did not actually send an SMS.
    if sent:
        return None
    return otp if settings.ENV != "prod" else None


async def verify_otp(phone: str, otp: str) -> bool:
    stored = await redis_client.get(f"otp:{phone}")
    if not stored:
        return False

    attempts = await redis_client.incr(f"otp:attempts:{phone}")
    if attempts > MAX_VERIFY_ATTEMPTS:
        await redis_client.delete(f"otp:{phone}", f"otp:attempts:{phone}")
        return False

    if secrets.compare_digest(stored, _hash_otp(phone, otp)):
        await redis_client.delete(f"otp:{phone}", f"otp:attempts:{phone}", f"otp:cooldown:{phone}")
        return True
    return False
