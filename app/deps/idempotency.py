"""Dependency that REQUIRES an Idempotency-Key header on a write.

The IdempotencyMiddleware already dedupes retries when a key is present but is opt-in;
money/stock-mutating endpoints (GRN receipt, stock adjust/wastage, batch receive) attach
this so a client MUST send a key — a network retry then replays the stored response
instead of double-applying the movement (golden rule #6)."""
from fastapi import Header, HTTPException, status


async def require_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "An Idempotency-Key header is required for this operation",
        )
    return idempotency_key
