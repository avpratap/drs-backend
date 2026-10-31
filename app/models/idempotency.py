"""Idempotency keys: dedupe client retries of write requests.

A client sends `Idempotency-Key: <uuid>` on a POST/PUT/PATCH/DELETE. The first
request runs and its response is stored; retries with the same key replay the
stored response instead of re-executing (so a flaky mobile network can't double
-charge or double-order). Reusing a key with a different body is rejected.
"""
import datetime as dt

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("key", name="uq_idempotency_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AppliedOperation(Base):
    """Handler-level record that a money operation ran for an Idempotency-Key.

    Written in the SAME transaction as the money movement, so it exists iff the movement
    committed. The middleware's claim is written in its own session before the handler;
    if the process dies between the money commit and the response store, the reaper
    clears that claim and a same-key retry would re-run the handler. Endpoints with no
    natural state guard (admin wallet topup/adjust, credit settle) check this record
    first and replay `result` instead of applying the movement twice."""

    __tablename__ = "applied_operations"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_applied_operation"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "wallet_topup"
    key: Mapped[str] = mapped_column(String(128), nullable=False)  # "<actor id>:<key>"
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
