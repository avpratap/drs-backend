import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import require_role
from app.models.notification import Notification
from app.models.user import User
from app.schemas.common import OutboxFlushOut
from app.schemas.delivery import CutoffResult
from app.services import forecasting
from app.services import notifications as notify
from app.services.cutoff import run_cutoff
from app.services.subscription import today_ist

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@router.post("/cutoff/run", response_model=CutoffResult, operation_id="run_cutoff")
async def run_cutoff_endpoint(date: dt.date | None = None, db: AsyncSession = Depends(get_db)):
    """Materialize active subscriptions into delivery_orders for a date (default tomorrow)."""
    delivery_date = date or (today_ist() + dt.timedelta(days=1))
    return await run_cutoff(db, delivery_date)


class NotifyTestIn(BaseModel):
    user_id: int
    event_type: str
    data: dict = Field(default_factory=dict)
    dedupe_key: str | None = None


class NotifyRowOut(BaseModel):
    id: int
    channel: str
    status: str
    skip_reason: str | None = None


class NotifyTestOut(BaseModel):
    created: list[NotifyRowOut]


class ForecastOut(BaseModel):
    for_date: str
    history_days: int
    plan: list[dict]


class NotificationOut(BaseModel):
    id: int
    user_id: int
    event_type: str
    channel: str
    category: str | None = None
    status: str
    skip_reason: str | None = None
    provider_message_id: str | None = None
    body: str | None = None
    attempts: int


@router.post(
    "/notifications/test", operation_id="emit_test_notification", response_model=NotifyTestOut
)
async def emit_test_notification(body: NotifyTestIn, db: AsyncSession = Depends(get_db)):
    """Emit an event for one user (gating applies) and return the rows created."""
    user = await db.get(User, body.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    rows = await notify.emit_event(
        db, body.event_type, user, body.data, dedupe_key=body.dedupe_key
    )
    await db.commit()
    return {
        "created": [
            {"id": r.id, "channel": r.channel, "status": r.status, "skip_reason": r.skip_reason}
            for r in rows
        ]
    }


@router.get("/forecast", operation_id="demand_forecast", response_model=ForecastOut)
async def demand_forecast(
    date: dt.date | None = None,
    history_days: int = 28,
    db: AsyncSession = Depends(get_db),
):
    """Next-day production plan: known committed standing orders + forecast of the
    variable (one-time) demand, with a service-level safety buffer, per product."""
    for_date = date or (today_ist() + dt.timedelta(days=1))
    history_days = min(max(history_days, 7), 365)
    plan = await forecasting.production_plan(db, for_date, history_days=history_days)
    return {"for_date": str(for_date), "history_days": history_days, "plan": plan}


@router.post(
    "/notifications/flush", operation_id="flush_notifications", response_model=OutboxFlushOut
)
async def flush_notifications(limit: int = 100, db: AsyncSession = Depends(get_db)):
    """Drain due queued notifications (also runs automatically in the ARQ worker)."""
    return await notify.process_outbox(db, limit=limit)


@router.get(
    "/notifications", operation_id="list_notifications", response_model=list[NotificationOut]
)
async def list_notifications(
    user_id: int | None = None,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    q = select(Notification).order_by(Notification.id.desc())
    if user_id is not None:
        q = q.where(Notification.user_id == user_id)
    if status_filter:
        q = q.where(Notification.status == status_filter)
    q = q.limit(min(max(limit, 1), 200)).offset(max(offset, 0))
    res = await db.execute(q)
    return [
        {
            "id": n.id,
            "user_id": n.user_id,
            "event_type": n.event_type,
            "channel": n.channel,
            "category": n.category,
            "status": n.status,
            "skip_reason": n.skip_reason,
            "provider_message_id": n.provider_message_id,
            "body": n.body,
            "attempts": n.attempts,
        }
        for n in res.scalars().all()
    ]
