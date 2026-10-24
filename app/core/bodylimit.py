"""Request-body size cap enforced on the bytes actually RECEIVED.

Checking only the Content-Length header is bypassed by `Transfer-Encoding: chunked`
(no header) — the idempotency middleware or an upload handler would then buffer an
arbitrarily large body in memory. This pure-ASGI middleware wraps `receive`, counts
body bytes, and answers 413 as soon as the cap is crossed.
"""
import json

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _BodyTooLarge(HTTPException):
    """An HTTPException so that when FastAPI itself is reading the body (request.body()/
    form() inside the route) it propagates as a proper 413 — FastAPI wraps any OTHER
    exception raised while reading the body into a generic 400."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


def _json(status: int, detail: str, code: str) -> tuple[list[tuple[bytes, bytes]], bytes]:
    body = json.dumps({"detail": detail, "error_code": code}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    return headers, body


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                too_big = int(cl) > self.max_bytes
            except ValueError:
                await self._reply(send, 400, "Invalid Content-Length", "bad_request")
                return
            if too_big:
                await self._reply(send, 413, "Request body too large", "payload_too_large")
                return

        received = 0
        started = False
        over = False  # the cap was crossed (whatever the app then made of the exception)

        async def counting_receive() -> Message:
            nonlocal received, over
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > self.max_bytes:
                    over = True
                    raise _BodyTooLarge
            return msg

        replaced = False

        async def tracking_send(msg: Message) -> None:
            nonlocal started, replaced
            if over and not started:
                # The app answered after our exception was wrapped (anyio task groups in
                # BaseHTTPMiddleware layers turn it into an ExceptionGroup, which FastAPI
                # maps to a generic 400): replace that response with the 413.
                started = replaced = True
                await self._reply(send, 413, "Request body too large", "payload_too_large")
                return
            if replaced:
                return  # swallow the rest of the replaced response
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._reply(send, 413, "Request body too large", "payload_too_large")
        except BaseExceptionGroup as eg:  # raised through anyio task groups
            if eg.subgroup(_BodyTooLarge) is None:
                raise
            if not started:
                await self._reply(send, 413, "Request body too large", "payload_too_large")

    @staticmethod
    async def _reply(send: Send, status: int, detail: str, code: str) -> None:
        headers, body = _json(status, detail, code)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
