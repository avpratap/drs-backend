"""Per-recipient send throttle for emails we send to an address the caller supplied
(email verification, newsletter confirmation): stops anyone from using our mailer to
flood a third party's inbox. Keyed by a hash of the recipient (no PII in Redis)."""
import hashlib
import logging

from app.core.redis import redis_client

log = logging.getLogger("drs.sendlimit")


async def allow_send(kind: str, recipient: str, *, cooldown_s: int = 60, per_day: int = 5) -> bool:
    key = hashlib.sha256(recipient.strip().lower().encode()).hexdigest()[:32]
    try:
        if not await redis_client.set(f"sendlim:{kind}:cd:{key}", 1, nx=True, ex=cooldown_s):
            return False
        n = await redis_client.incr(f"sendlim:{kind}:day:{key}")
        if n == 1:
            await redis_client.expire(f"sendlim:{kind}:day:{key}", 86400)
        return n <= per_day
    except Exception:  # Redis down: don't block legitimate confirmation mail
        log.warning("send limiter unavailable; allowing %s send", kind)
        return True
