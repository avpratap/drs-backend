"""Idempotency-Key middleware.

Opt-in per request: a client that sends `Idempotency-Key: <uuid>` on a write
gets at-most-once execution. The first call runs and its 2xx response is stored;
retries replay the stored response. Non-2xx responses release the key so the
client can genuinely retry. Reusing a key with a different body is a 422.
"""
import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.db import SessionLocal
from app.core.security import decode_token
from app.models.idempotency import IdempotencyKey

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_MAX_KEY_LEN = 100  # stored as "<sub>:<key>" in a String(128) column
# Never persist these responses (they carry access/refresh tokens) for 48h replay.
_SKIP_PREFIXES = ("/api/v1/auth/",)
_HOP_HEADERS = {"content-length", "content-type"}


def _subject(request: Request) -> str:
    """Best-effort caller id from the bearer token, to scope keys per user."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            return str(decode_token(auth[7:]).get("sub") or "anon")
        except Exception:
            return "anon"
    return "anon"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        raw_key = request.headers.get("Idempotency-Key")
        if request.method not in _WRITE_METHODS or not raw_key:
            return await call_next(request)
        if request.url.path.startswith(_SKIP_PREFIXES):
            return await call_next(request)
        if len(raw_key) > _MAX_KEY_LEN:
            # A longer key overflowed the column → DataError 500.
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Idempotency-Key must be at most {_MAX_KEY_LEN} characters",
                    "error_code": "bad_request",
                },
            )
        # Scope the key to the caller so two users can't collide / replay each other.
        key = f"{_subject(request)}:{raw_key}"

        body = await request.body()
        # Re-inject the body so downstream handlers can read it again.
        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _receive

        path = request.url.path[:255]  # column width; the hash below covers the full path
        req_hash = hashlib.sha256(
            b"%s:%s:%s" % (request.method.encode(), request.url.path.encode(), body)
        ).hexdigest()

        # Try to claim the key (unique constraint arbitrates concurrent claims).
        claimed = False
        async with SessionLocal() as db:
            db.add(
                IdempotencyKey(
                    key=key, method=request.method, path=path, request_hash=req_hash
                )
            )
            try:
                await db.commit()
                claimed = True
            except IntegrityError:
                await db.rollback()

            if not claimed:
                existing = await db.scalar(
                    select(IdempotencyKey).where(IdempotencyKey.key == key)
                )
                if existing is None:
                    pass  # extremely rare race; fall through and execute
                elif existing.request_hash != req_hash:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": "Idempotency-Key reused with a different request"},
                    )
                elif existing.response_status is None:
                    # In progress → 409. We deliberately do NOT auto-reclaim a "stale"
                    # claim in-request: a handler merely slow (gateway latency) is still
                    # running, so re-executing it would double-charge, and a naive UPDATE
                    # reclaim is a TOCTOU race where two retries both proceed. Orphaned
                    # claims from a true crash are cleared out-of-band by the maintenance
                    # reaper (reap_stale_idempotency), never by re-running the money path.
                    return JSONResponse(
                        status_code=409,
                        content={"detail": "A request with this Idempotency-Key is in progress"},
                    )
                else:
                    return Response(
                        content=existing.response_body or "",
                        status_code=existing.response_status,
                        media_type=existing.response_media_type,
                        headers={"Idempotent-Replay": "true"},
                    )

        # We hold the claim: execute and buffer the response. If the handler raises an
        # unhandled error (e.g. a transient gateway/DB fault that Starlette turns into a
        # 500), release the claim so the client can genuinely retry instead of being
        # wedged on a 409 "in progress" until the 48h prune.
        try:
            response = await call_next(request)
        except Exception:
            async with SessionLocal() as db_err:
                row = await db_err.scalar(
                    select(IdempotencyKey).where(IdempotencyKey.key == key)
                )
                if row is not None and row.response_status is None:
                    await db_err.delete(row)
                    await db_err.commit()
            raise
        chunks = [chunk async for chunk in response.body_iterator]
        out = b"".join(chunks)

        async with SessionLocal() as db:
            row = await db.scalar(select(IdempotencyKey).where(IdempotencyKey.key == key))
            if row is not None:
                if 200 <= response.status_code < 300:
                    row.response_status = response.status_code
                    row.response_body = out.decode("utf-8", "replace")
                    row.response_media_type = (
                        response.media_type or response.headers.get("content-type")
                    )
                    await db.commit()
                else:
                    await db.delete(row)  # release so a real retry can proceed
                    await db.commit()

        headers = {k: v for k, v in response.headers.items() if k.lower() not in _HOP_HEADERS}
        return Response(
            content=out,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )
