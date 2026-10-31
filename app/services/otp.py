"""Phone OTP via Redis.

Dev: the OTP is returned in the API response so you can test without MSG91.
Prod: wire send_sms() to MSG91 (DLT-registered) and never return the OTP.
"""
import asyncio
import hashlib
import secrets

from app.core.config import settings
from app.core.redis import incr_window, redis_client
from app.services.sms import send_otp_sms, sms_enabled

# Floor at 60s so a misconfigured MSG91_OTP_EXPIRY_MIN=0 can't make every OTP expire
# instantly (redis SET ex=0 is rejected) and lock users out of login.
OTP_TTL = max(60, settings.MSG91_OTP_EXPIRY_MIN * 60)  # keep in sync with the SMS's stated expiry
# Per (phone, requesting IP): so someone else hammering a victim's number can't use up
# the victim's own budget (the SMS always goes to the phone's owner anyway).
MAX_SENDS_PER_HOUR = 5
MAX_SENDS_PER_PHONE_HOUR = 15  # per phone across all IPs: bounds SMS cost/flooding
MAX_SENDS_PER_IP_HOUR = 50  # across all phones from one device/IP
MAX_VERIFY_ATTEMPTS = 5  # per issued OTP, per phone: THE hard brute-force cap
# Per-IP failed verifies (across all phones) are a throttle, not a block: many Indian
# users share one CGNAT/office IP, so a block would lock out people typing the RIGHT code.
VERIFY_THROTTLE_AFTER_FAILS = 50  # past this, every verify from the IP is slowed...
VERIFY_THROTTLE_DELAY_S = 2.0  # ...by this much (a correct code still succeeds)
VERIFY_FAILS_PER_IP_CEILING = 2000  # hard 429 only at a volume no shared NAT produces
RESEND_COOLDOWN = 60  # seconds


def _hash_otp(phone: str, otp: str) -> str:
    return hashlib.sha256(f"{phone}:{otp}:{settings.JWT_SECRET}".encode()).hexdigest()


class UseLatestCode(Exception):
    """The send cap for this phone is reached but a code sent earlier is still valid:
    tell the caller to use the latest SMS instead of failing (so a third party requesting
    OTPs for someone's number can't lock them out of their own login)."""


async def _capped(phone: str) -> None:
    if await redis_client.exists(f"otp:{phone}"):
        raise UseLatestCode()
    raise ValueError("Too many OTP requests. Try again later.")


async def request_otp(phone: str, ip: str | None = None) -> str | None:
    """Generate + store an OTP. Returns the OTP in non-prod for testing, else None.
    Raises ValueError (429) when rate-limited with no valid code outstanding, and
    UseLatestCode when capped but the last code sent to the phone is still valid."""
    # Cooldown first so a client that's merely resending too fast doesn't burn its hourly
    # per-IP budget (the IP counter only increments once we're actually going to send).
    if await redis_client.get(f"otp:cooldown:{phone}"):
        raise ValueError("Please wait a moment before requesting another OTP")

    # Per-IP cap — stops a single device cycling through many phone numbers.
    if ip:
        if await incr_window(f"otp:ip:{ip}", 3600) > MAX_SENDS_PER_IP_HOUR:
            raise ValueError("Too many OTP requests from this device. Try again later.")

    # Per (phone, IP) cap, then a looser per-phone ceiling across all callers.
    if await incr_window(f"otp:send:{phone}:{ip or '-'}", 3600) > MAX_SENDS_PER_HOUR:
        await _capped(phone)
    if await incr_window(f"otp:send:{phone}", 3600) > MAX_SENDS_PER_PHONE_HOUR:
        await _capped(phone)

    otp = f"{secrets.randbelow(10**6):06d}"
    await redis_client.set(f"otp:{phone}", _hash_otp(phone, otp), ex=OTP_TTL)
    await redis_client.set(f"otp:attempts:{phone}", 0, ex=OTP_TTL)
    await redis_client.set(f"otp:cooldown:{phone}", "1", ex=RESEND_COOLDOWN)

    sent = await send_otp_sms(phone, otp) if sms_enabled() else False
    # Return the OTP for testing ONLY when we did not actually send an SMS.
    if sent:
        return None
    return otp if settings.ENV != "prod" else None


# Compare-and-delete in one atomic step (Redis runs a script without interleaving).
_CONSUME_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


async def _verify_failed(ip: str | None) -> bool:
    """Count a failed verify against the caller's IP; always returns False."""
    if ip:
        await incr_window(f"otp:vfail:{ip}", 3600)
    return False


async def verify_otp(phone: str, otp: str, ip: str | None = None) -> bool:
    """True when `otp` is the outstanding code for `phone` (consumed on success).

    The hard limit is per phone (MAX_VERIFY_ATTEMPTS per issued code). Failed verifies
    are also counted per IP: past VERIFY_THROTTLE_AFTER_FAILS in the hour each verify
    from that IP is delayed (slowing a guesser cycling through many phones) but still
    evaluated, so co-NAT users submitting a correct code are never locked out. Only at
    VERIFY_FAILS_PER_IP_CEILING does it raise ValueError (429)."""
    if ip:
        fails = int(await redis_client.get(f"otp:vfail:{ip}") or 0)
        if fails >= VERIFY_FAILS_PER_IP_CEILING:
            raise ValueError("Too many incorrect codes from this network. Try again later.")
        if fails >= VERIFY_THROTTLE_AFTER_FAILS:
            await asyncio.sleep(VERIFY_THROTTLE_DELAY_S)
    stored = await redis_client.get(f"otp:{phone}")
    if not stored:
        return await _verify_failed(ip)

    attempts = await redis_client.incr(f"otp:attempts:{phone}")
    if attempts > MAX_VERIFY_ATTEMPTS:
        await redis_client.delete(f"otp:{phone}", f"otp:attempts:{phone}")
        return await _verify_failed(ip)

    expected = _hash_otp(phone, otp)
    if not secrets.compare_digest(stored, expected):
        return await _verify_failed(ip)
    # Consume atomically: only the request that actually deletes the stored hash wins, so
    # two concurrent verifies with the right code can't BOTH log in (replay race).
    consumed = await redis_client.eval(_CONSUME_LUA, 1, f"otp:{phone}", expected)
    if not consumed:
        return False
    await redis_client.delete(f"otp:attempts:{phone}", f"otp:cooldown:{phone}")
    return True
