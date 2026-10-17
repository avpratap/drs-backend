"""Customer support tickets + threaded messages (complaint inbox)."""
import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# open -> pending (awaiting customer after an admin reply) -> open (customer replied)
#       -> resolved -> closed
TICKET_STATUSES = ("open", "pending", "resolved", "closed")


class SupportTicket(Base, TimestampMixin):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=True
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(
        String(16), default="other", nullable=False
    )  # order|delivery|payment|product|other
    status: Mapped[str] = mapped_column(String(12), default="open", nullable=False)
    priority: Mapped[str] = mapped_column(String(8), default="normal", nullable=False)  # low|normal|high


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sender_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    sender_role: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
