"""Newsletter subscription (public opt-in/out) + admin subscriber list."""
import datetime as dt

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.newsletter import NewsletterSubscriber

router = APIRouter(tags=["newsletter"])


_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class SubscribeIn(BaseModel):
    email: str = Field(pattern=_EMAIL, max_length=255)
    name: str | None = Field(default=None, max_length=120)


class UnsubscribeIn(BaseModel):
    email: str = Field(pattern=_EMAIL, max_length=255)


class SubscriberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str | None = None
    status: str


class StatusOut(BaseModel):
    email: str
    status: str


@router.post("/newsletter/subscribe", response_model=StatusOut, operation_id="newsletter_subscribe")
async def subscribe(data: SubscribeIn, db: AsyncSession = Depends(get_db)):
    """Idempotent: a repeat subscribe (or re-subscribe after opt-out) reactivates."""
    email = str(data.email).lower()
    row = await db.scalar(select(NewsletterSubscriber).where(NewsletterSubscriber.email == email))
    if row is None:
        row = NewsletterSubscriber(
            email=email, name=data.name, status="subscribed",
            source="web", consent_at=dt.datetime.now(dt.UTC),
        )
        db.add(row)
    else:
        row.status = "subscribed"
        row.consent_at = dt.datetime.now(dt.UTC)
        if data.name:
            row.name = data.name
    await db.commit()
    return StatusOut(email=email, status="subscribed")


@router.post("/newsletter/unsubscribe", response_model=StatusOut, operation_id="newsletter_unsubscribe")
async def unsubscribe(data: UnsubscribeIn, db: AsyncSession = Depends(get_db)):
    email = str(data.email).lower()
    row = await db.scalar(select(NewsletterSubscriber).where(NewsletterSubscriber.email == email))
    if row is not None:
        row.status = "unsubscribed"
        await db.commit()
    return StatusOut(email=email, status="unsubscribed")


@router.get(
    "/admin/newsletter/subscribers",
    response_model=list[SubscriberOut],
    operation_id="list_newsletter_subscribers",
    dependencies=[Depends(require_role("admin"))],
)
async def list_subscribers(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(NewsletterSubscriber).order_by(NewsletterSubscriber.id.desc())
    if status_filter:
        q = q.where(NewsletterSubscriber.status == status_filter)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()
