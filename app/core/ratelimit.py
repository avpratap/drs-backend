"""Global per-IP rate limiting (Redis fixed-window), fail-open.

A coarse abuse/DoS guard in front of every endpoint; per-endpoint limits (OTP) stay
where they are. The client IP comes from app.core.clientip (proxy-hop aware, not
spoofable via a client-supplied X-Forwarded-For).
"""
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.clientip import client_ip
from app.core.config import settings
from app.core.redis import redis_client

log = logging.getLogger("drs.ratelimit")
_SKIP = {"/healthz", "/readyz", "/metrics"}


async def allow(ip: str, limit: int, window: int = 60) -> bool:
    """Fixed-window counter. Returns True if under the limit (fail-open on error)."""
    try:
        bucket = int(time.time() // window)
        key = f"rl:{ip}:{bucket}"
        # INCR + EXPIRE in one MULTI: an EXPIRE lost after INCR left a key with no TTL.
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window)
            n, _ = await pipe.execute()
        return n <= limit
    except Exception as exc:
        log.warning("rate-limit check failed (open): %s", exc)
        return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.RATE_LIMIT_ENABLED or request.url.path in _SKIP:
            return await call_next(request)
        ip = client_ip(request) or "unknown"
        if not await allow(ip, settings.RATE_LIMIT_PER_MIN):
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
        return await call_next(request)
