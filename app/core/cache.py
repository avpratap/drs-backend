"""Tiny cache-aside helper over Redis.

Fail-open: if Redis is unavailable the helpers degrade to a cache miss / no-op so
a cache outage never takes down request handling.
"""
import json
import logging

from app.core.config import settings
from app.core.redis import redis_client

log = logging.getLogger("drs.cache")


async def cache_get_json(key: str):
    try:
        raw = await redis_client.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:
        log.warning("cache get failed (%s): %s", key, exc)
        return None


async def cache_set_json(key: str, value, ttl: int | None = None) -> None:
    try:
        await redis_client.set(
            key, json.dumps(value, default=str), ex=ttl or settings.CACHE_TTL_SECONDS
        )
    except Exception as exc:
        log.warning("cache set failed (%s): %s", key, exc)


async def cache_delete(*keys: str) -> None:
    if not keys:
        return
    try:
        await redis_client.delete(*keys)
    except Exception as exc:
        log.warning("cache delete failed: %s", exc)
