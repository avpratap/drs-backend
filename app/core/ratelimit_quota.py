"""Per-user daily upload quota (the bucket is not free image hosting)."""
import datetime as dt
import logging

from app.core.redis import redis_client

log = logging.getLogger("drs.quota")


async def upload_quota_ok(user_id: int, *, per_day: int = 100) -> bool:
    key = f"uploadq:{user_id}:{dt.date.today().isoformat()}"
    try:
        n = await redis_client.incr(key)
        if n == 1:
            await redis_client.expire(key, 2 * 86400)
        return n <= per_day
    except Exception:
        log.warning("upload quota unavailable; allowing upload")
        return True
