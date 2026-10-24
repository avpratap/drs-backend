"""Trustworthy client IP behind the platform proxy.

uvicorn's --forwarded-allow-ips '*' trusts EVERY X-Forwarded-For hop and reports the
LEFTMOST entry — which the client controls, so rotating a fake header bypassed the per-IP
OTP cap and the global rate limit (and forged AuthSession/Consent IPs). Instead we count
hops from the RIGHT: the platform proxy appends the real peer address, so with
TRUSTED_PROXY_HOPS=N the client is the N-th entry from the end. With no trusted proxy
(N=0, local/dev/tests) the socket peer is used and the header is ignored.
"""
from starlette.requests import Request

from app.core.config import settings


def client_ip(request: Request) -> str | None:
    hops = settings.TRUSTED_PROXY_HOPS
    if hops > 0:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if len(parts) >= hops:
                return parts[-hops][:64]
    return request.client.host if request.client else None
