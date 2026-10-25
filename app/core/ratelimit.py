"""Global per-IP rate limiting (Redis fixed-window), fail-open.

A coarse abuse/DoS guard in front of every endpoint; per-endpoint limits (OTP) stay
where they are. The client IP comes from app.core.clientip (proxy-hop aware, not
spoofable via a client-supplied X-Forwarded-For).

Authenticated ADMIN requests are exempt (bulk admin / batch work must not be throttled
by a per-IP limit meant for anonymous abuse). The exemption is only granted after the
token's signature AND the user's current role/status/token version are checked against
the database — a forged, stale or revoked "admin" token is counted like anyone else.
Anonymous and non-admin requests keep the limit.
"""
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.clientip import client_ip
from app.core.config import settings
from app.core.redis import redis_client
from app.core.security import decode_token

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


async def is_verified_admin(request: Request) -> bool:
    """True only for a valid access token of a CURRENTLY active admin."""
    auth = request.headers.get("authorization") or ""
    if not auth[:7].lower() == "bearer ":
        return False
    try:
        payload = decode_token(auth[7:].strip())
        if payload.get("type") != "access" or payload.get("role") != "admin":
            return False  # cheap pre-filter: only claimed admins cost a DB lookup
        user_id = int(payload["sub"])
    except Exception:
        return False
    try:
        from app.core.db import SessionLocal
        from app.models.user import User

        async with SessionLocal() as db:
            user = await db.get(User, user_id)
        return (
            user is not None
            and user.role == "admin"
            and user.status == "active"
            and user.token_version == payload.get("ver")
        )
    except Exception as exc:
        log.warning("admin rate-limit exemption check failed (limit applies): %s", exc)
        return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.RATE_LIMIT_ENABLED or request.url.path in _SKIP:
            return await call_next(request)
        if await is_verified_admin(request):
            return await call_next(request)  # admins aren't counted against the IP limit
        ip = client_ip(request) or "unknown"
        if not await allow(ip, settings.RATE_LIMIT_PER_MIN):
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
        return await call_next(request)
