"""In-app notification inbox: the recipient reads their own notifications + marks read.

Complements the outbound channels (push/SMS/WhatsApp/email) with a bell-UI surface.
"""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.models.notification import Notification
from app.models.user import User

router = APIRouter(prefix="/notifications", tags=["inbox"])


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_type: str
    channel: str
    category: str
    body: str | None = None
    status: str
    created_at: dt.datetime
    read_at: dt.datetime | None = None


class UnreadCountOut(BaseModel):
    unread: int


@router.get("", response_model=list[NotificationOut], operation_id="list_my_notifications")
async def list_my_notifications(
    unread_only: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    # The in-app bell = push-channel rows, EXCLUDING only those skipped because the user
    # turned push OFF (channel_off). A no-device "skipped" (no_address) or an FCM-delivery
    # "failed" row is still a valid in-app message — the bell doesn't depend on FCM.
    q = select(Notification).where(
        Notification.user_id == user.id,
        Notification.channel == "push",
        or_(Notification.skip_reason.is_(None), Notification.skip_reason != "channel_off"),
    )
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    q = q.order_by(Notification.id.desc()).limit(page.limit).offset(page.offset)
    return (await db.execute(q)).scalars().all()


@router.get("/unread-count", response_model=UnreadCountOut, operation_id="unread_notification_count")
async def unread_count(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    n = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == user.id,
            Notification.channel == "push",
            or_(Notification.skip_reason.is_(None), Notification.skip_reason != "channel_off"),
            Notification.read_at.is_(None),
        )
    )
    return UnreadCountOut(unread=int(n or 0))


@router.post("/{notification_id}/read", response_model=NotificationOut, operation_id="mark_notification_read")
async def mark_read(
    notification_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    row = await db.get(Notification, notification_id)
    # 404 (not 403) on someone else's notification so ids aren't enumerable.
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    if row.read_at is None:
        row.read_at = dt.datetime.now(dt.UTC)
        await db.commit()
        await db.refresh(row)
    return row


@router.post("/read-all", response_model=UnreadCountOut, operation_id="mark_all_notifications_read")
async def mark_all_read(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import update

    await db.execute(
        update(Notification)
        .where(
            Notification.user_id == user.id,
            Notification.channel == "push",
            or_(Notification.skip_reason.is_(None), Notification.skip_reason != "channel_off"),
            Notification.read_at.is_(None),
        )
        .values(read_at=dt.datetime.now(dt.UTC))
    )
    await db.commit()
    return UnreadCountOut(unread=0)
