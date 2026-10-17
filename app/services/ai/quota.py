"""Per-user daily AI call quota (Redis counter, fail-open).

Bounds cost/abuse on customer-facing AI endpoints: adaptive thinking is expensive,
so a flooded /ai/support could burn budget. Counter resets daily (IST). If Redis is
down we fail open (allow) — availability over strict enforcement.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.redis import redis_client


async def check_and_increment(user_id: int, *, scope: str = "ai") -> None:
    limit = settings.AI_DAILY_CALLS_PER_USER
    if limit <= 0:
        return
    today = dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date().isoformat()
    key = f"aiquota:{scope}:{user_id}:{today}"
    try:
        n = await redis_client.incr(key)
        # Set the TTL EVERY call (not just n==1): if the first EXPIRE ever failed the key
        # would otherwise live forever with no reset. expire() is idempotent and cheap.
        await redis_client.expire(key, 60 * 60 * 25)  # ~1 day + slack
    except Exception:
        return  # fail-open
    if n > limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Daily AI limit reached ({limit}/day). Please try again tomorrow.",
        )
