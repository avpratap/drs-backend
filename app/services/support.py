"""Support-ticket lifecycle: open, reply (threaded), status transitions."""
from uuid import uuid4

from fastapi import HTTPException
from fastapi import status as http
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.shop import CustomerAccount
from app.models.support import TICKET_STATUSES, SupportTicket, TicketMessage
from app.models.user import User

_TERMINAL = {"resolved", "closed"}


async def _account_id_for(db: AsyncSession, user: User) -> int | None:
    return await db.scalar(
        select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id).limit(1)
    )


async def create_ticket(db: AsyncSession, user: User, subject: str, category: str, body: str) -> SupportTicket:
    ticket = SupportTicket(
        ticket_no=f"PENDING-{uuid4().hex[:20]}",
        account_id=await _account_id_for(db, user),
        created_by=user.id,
        subject=subject,
        category=category,
        status="open",
    )
    db.add(ticket)
    await db.flush()
    ticket.ticket_no = f"TKT{ticket.id:06d}"
    db.add(
        TicketMessage(
            ticket_id=ticket.id, sender_user_id=user.id, sender_role=user.role, body=body
        )
    )
    return ticket


async def add_message(
    db: AsyncSession, ticket: SupportTicket, user: User, body: str, attachment_url: str | None = None
) -> TicketMessage:
    if ticket.status == "closed":
        raise HTTPException(http.HTTP_409_CONFLICT, "Ticket is closed")
    msg = TicketMessage(
        ticket_id=ticket.id,
        sender_user_id=user.id,
        sender_role=user.role,
        body=body,
        attachment_url=attachment_url,
    )
    db.add(msg)
    # An admin reply puts the ball in the customer's court; a customer reply reopens.
    if user.role == "admin":
        if ticket.status != "closed":
            ticket.status = "pending"
    elif ticket.status in ("pending", "resolved"):
        ticket.status = "open"
    return msg


async def set_status(db: AsyncSession, ticket: SupportTicket, new_status: str) -> SupportTicket:
    if new_status not in TICKET_STATUSES:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid status")
    ticket.status = new_status
    return ticket
