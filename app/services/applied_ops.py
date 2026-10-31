"""Handler-level idempotency for money endpoints (see models.idempotency.AppliedOperation).

Usage in a handler (inside its transaction):

    done = await applied_ops.replay(db, "wallet_topup", key, payload)
    if done is not None:
        return done                      # already applied: same result, nothing moved
    ...move money, build result...
    await applied_ops.record(db, "wallet_topup", key, payload, result)
    await db.commit()                    # movement + record commit together
"""
import hashlib
import json

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import AppliedOperation


def scoped_key(actor_id: int, idempotency_key: str) -> str:
    return f"{actor_id}:{idempotency_key}"[:128]


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


async def replay(db: AsyncSession, scope: str, key: str, payload: dict) -> dict | None:
    """The stored result if this (scope, key) already ran; 422 if it ran with a
    different request. Locks nothing: the unique constraint arbitrates a race."""
    row = await db.scalar(
        select(AppliedOperation).where(AppliedOperation.scope == scope, AppliedOperation.key == key)
    )
    if row is None:
        return None
    if row.request_hash != _hash(payload):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Idempotency-Key reused with a different request"
        )
    return row.result


async def record(db: AsyncSession, scope: str, key: str, payload: dict, result: dict) -> None:
    """Add the record to the current transaction, flushed in a savepoint so a concurrent
    duplicate fails here on the unique constraint (409, the caller's movement then rolls
    back with the request) rather than surfacing as a 500."""
    try:
        async with db.begin_nested():
            db.add(AppliedOperation(scope=scope, key=key, request_hash=_hash(payload), result=result))
    except IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A request with this Idempotency-Key was already applied"
        ) from None
