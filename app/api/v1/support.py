"""Support tickets: customer inbox + admin console."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import media
from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, paged, pagination
from app.models.support import SupportTicket, TicketMessage
from app.models.user import User
from app.services import support

router = APIRouter(tags=["support"])


# ---------- Schemas ----------
class TicketCreateIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    category: str = Field(default="other", pattern="^(order|delivery|payment|product|other)$")
    body: str = Field(min_length=1, max_length=5000)


def _safe_attachment_url(v: str | None) -> str | None:
    """Attachments are shown in the admin console and the customer inbox, so a
    `javascript:`/`data:` URL would be stored-XSS. Allow only an absolute https URL
    (our CDN) or a same-origin relative path (not protocol-relative `//host`)."""
    if v is None:
        return v
    s = v.strip()
    if media.is_ref(s):
        return s  # a private upload reference (ownership checked by the endpoint)
    if s.lower().startswith("https://"):
        return v
    if s.startswith("/") and not s.startswith(("//", "/\\")):
        return v
    raise ValueError("attachment_url must be an absolute https URL or a same-origin path")


def _own_attachment(value: str | None, user: User) -> None:
    """A private upload may be attached only by the user who uploaded it."""
    if media.is_ref(value) and not media.ref_owned(value, "uploads", user.id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown attachment")


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    attachment_url: str | None = Field(default=None, max_length=512)

    _v_attach = field_validator("attachment_url")(lambda v: _safe_attachment_url(v))


class StatusIn(BaseModel):
    status: str = Field(pattern="^(open|pending|resolved|closed)$")


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sender_role: str
    body: str
    attachment_url: str | None = None

    @field_serializer("attachment_url")
    def _view_url(self, v: str | None) -> str | None:
        return media.resolve(v)  # private upload -> short-lived signed URL


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ticket_no: str
    subject: str
    category: str
    status: str
    priority: str
    messages: list[MessageOut] = []


_THREAD_CAP = 1000  # a single thread view is bounded too


async def _messages(db: AsyncSession, ticket_id: int) -> list[MessageOut]:
    rows = (
        await db.execute(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.id.desc())  # the NEWEST _THREAD_CAP, shown oldest-first
            .limit(_THREAD_CAP)
        )
    ).scalars().all()
    return [MessageOut.model_validate(m) for m in reversed(rows)]


_LIST_MSG_CAP = 50  # list views carry the latest messages only (full thread on GET by id)


async def _with_messages_many(db: AsyncSession, tickets: list[SupportTicket]) -> list[TicketOut]:
    """List views with ONE batched message query (was a query per ticket — N+1), capped
    per ticket so a long thread can't bloat a 200-row admin page."""
    if not tickets:
        return []
    by_ticket: dict[int, list[MessageOut]] = {}
    # Cap IN the query (latest N per ticket via a window), not after loading every
    # message of every ticket into memory.
    rn = (
        func.row_number()
        .over(partition_by=TicketMessage.ticket_id, order_by=TicketMessage.id.desc())
        .label("rn")
    )
    latest = (
        select(TicketMessage.id, rn)
        .where(TicketMessage.ticket_id.in_([t.id for t in tickets]))
        .subquery()
    )
    for m in (
        await db.execute(
            select(TicketMessage)
            .join(latest, latest.c.id == TicketMessage.id)
            .where(latest.c.rn <= _LIST_MSG_CAP)
            .order_by(TicketMessage.ticket_id, TicketMessage.id)
        )
    ).scalars():
        by_ticket.setdefault(m.ticket_id, []).append(MessageOut.model_validate(m))
    outs = []
    for t in tickets:
        o = TicketOut.model_validate(t)
        o.messages = by_ticket.get(t.id, [])
        outs.append(o)
    return outs


async def _with_messages(db: AsyncSession, ticket: SupportTicket) -> TicketOut:
    out = TicketOut.model_validate(ticket)
    out.messages = await _messages(db, ticket.id)
    return out


async def _owned_or_404(db: AsyncSession, ticket_id: int, user: User) -> SupportTicket:
    ticket = await db.get(SupportTicket, ticket_id)
    # 404 (not 403) on someone else's ticket so ids aren't enumerable.
    if ticket is None or ticket.created_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return ticket


# ---------- Customer ----------
@router.post("/support/tickets", response_model=TicketOut, operation_id="create_ticket")
async def create_ticket(
    data: TicketCreateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    ticket = await support.create_ticket(db, user, data.subject, data.category, data.body)
    await db.commit()
    await db.refresh(ticket)
    return await _with_messages(db, ticket)


@router.get("/support/tickets", response_model=list[TicketOut], operation_id="list_my_tickets")
async def list_my_tickets(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    rows = (
        await db.execute(
            select(SupportTicket)
            .where(SupportTicket.created_by == user.id)
            .order_by(SupportTicket.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
    ).scalars().all()
    return await _with_messages_many(db, list(rows))


@router.get("/support/tickets/{ticket_id}", response_model=TicketOut, operation_id="get_ticket")
async def get_ticket(
    ticket_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    ticket = await _owned_or_404(db, ticket_id, user)
    return await _with_messages(db, ticket)


@router.post(
    "/support/tickets/{ticket_id}/messages", response_model=TicketOut, operation_id="reply_ticket"
)
async def reply_ticket(
    ticket_id: int,
    data: MessageIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ticket = await _owned_or_404(db, ticket_id, user)
    _own_attachment(data.attachment_url, user)
    await support.add_message(db, ticket, user, data.body, data.attachment_url)
    await db.commit()
    await db.refresh(ticket)
    return await _with_messages(db, ticket)


# ---------- Admin ----------
@router.get(
    "/admin/support/tickets",
    response_model=list[TicketOut],
    operation_id="admin_list_tickets",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_list_tickets(
    status_filter: str | None = None,
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(SupportTicket).order_by(SupportTicket.id.desc())
    if status_filter:
        q = q.where(SupportTicket.status == status_filter)
    if category:
        q = q.where(SupportTicket.category == category)
    rows = await paged(db, q, page)
    return await _with_messages_many(db, list(rows))


@router.post(
    "/admin/support/tickets/{ticket_id}/messages",
    response_model=TicketOut,
    operation_id="admin_reply_ticket",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_reply_ticket(
    ticket_id: int,
    data: MessageIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ticket = await db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    _own_attachment(data.attachment_url, user)
    await support.add_message(db, ticket, user, data.body, data.attachment_url)
    await db.commit()
    await db.refresh(ticket)
    return await _with_messages(db, ticket)


@router.post(
    "/admin/support/tickets/{ticket_id}/status",
    response_model=TicketOut,
    operation_id="admin_set_ticket_status",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_set_ticket_status(
    ticket_id: int, data: StatusIn, db: AsyncSession = Depends(get_db)
):
    ticket = await db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    await support.set_status(db, ticket, data.status)
    await db.commit()
    await db.refresh(ticket)
    return await _with_messages(db, ticket)
