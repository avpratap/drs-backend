"""Per-user daily upload quota (the bucket is not free image hosting)."""
import datetime as dt
import logging

from app.core.redis import incr_window

log = logging.getLogger("drs.quota")


async def upload_quota_ok(user_id: int, *, per_day: int = 100) -> bool:
    key = f"uploadq:{user_id}:{dt.date.today().isoformat()}"
    try:
        return await incr_window(key, 2 * 86400) <= per_day
    except Exception:
        log.warning("upload quota unavailable; allowing upload")
        return True
