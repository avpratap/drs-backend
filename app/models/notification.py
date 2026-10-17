"""Notification outbox: one row per (recipient, channel, event).

A durable log + queue. emit_event() writes rows (status 'queued' or 'skipped'),
the ARQ worker (or the admin flush endpoint) delivers 'queued' rows that are due,
and every attempt is recorded for auditability and retry.
"""
import datetime as dt

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_accounts.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    channel: Mapped[str] = mapped_column(String(12), nullable=False)  # whatsapp|sms|push|email
    category: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # transactional|marketing
    template_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_addr: Mapped[str | None] = mapped_column(String(255), nullable=True)  # phone/token/email
    body: Mapped[str | None] = mapped_column(Text, nullable=True)  # rendered text
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(12), default="queued", nullable=False, index=True
    )  # queued|sent|failed|skipped
    skip_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Dedupe key for idempotent emits (e.g. "payment.success:42").
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Retry backoff: a failed delivery is re-attempted no earlier than next_attempt_at.
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
