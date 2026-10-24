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


class ProxySchemeMiddleware:
    """uvicorn runs with --no-proxy-headers (so a client can't spoof its IP), which also
    drops X-Forwarded-Proto: behind the TLS-terminating proxy every request looked like
    http, and redirects (e.g. trailing-slash 307s) pointed at http:// — mixed content
    that browsers block. Only when a trusted proxy is configured, take the scheme the
    proxy reports (its own, i.e. the right-most, value)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and settings.TRUSTED_PROXY_HOPS > 0:
            for name, value in scope.get("headers") or []:
                if name == b"x-forwarded-proto":
                    proto = value.decode("latin-1").split(",")[-1].strip().lower()
                    if proto in ("http", "https"):
                        ws = scope["type"] == "websocket"
                        scope = dict(scope, scheme=("wss" if proto == "https" else "ws") if ws else proto)
                    break
        await self.app(scope, receive, send)
