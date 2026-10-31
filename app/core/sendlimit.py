"""Per-recipient send throttle for emails we send to an address the caller supplied
(email verification, newsletter confirmation): stops anyone from using our mailer to
flood a third party's inbox. Keyed by hashes (no PII in Redis).

The tight cooldown + daily cap are per (sender, recipient) — sender is the user id or
client IP — so one party claiming a victim's address can't lock the REAL owner out of
their own confirmation mail; a looser per-recipient daily ceiling still bounds the total
any number of senders can push at one inbox."""
import hashlib
import logging

from app.core.redis import incr_window, redis_client

log = logging.getLogger("drs.sendlimit")

RECIPIENT_DAILY_CEILING = 20


def _h(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:32]


async def _incr_day(key: str) -> int:
    return await incr_window(key, 86400)


async def allow_send(
    kind: str, recipient: str, *, sender: str = "", cooldown_s: int = 60, per_day: int = 5
) -> bool:
    pair = _h(f"{sender}|{recipient}")
    rcpt = _h(recipient)
    try:
        if not await redis_client.set(f"sendlim:{kind}:cd:{pair}", 1, nx=True, ex=cooldown_s):
            return False
        if await _incr_day(f"sendlim:{kind}:day:{pair}") > per_day:
            return False
        return await _incr_day(f"sendlim:{kind}:rcpt:{rcpt}") <= RECIPIENT_DAILY_CEILING
    except Exception:  # Redis down: don't block legitimate confirmation mail
        log.warning("send limiter unavailable; allowing %s send", kind)
        return True
