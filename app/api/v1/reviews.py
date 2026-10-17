"""Admin review moderation: approve/reject/hide + merchant reply."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete
from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.rating import REVIEW_STATUSES, Rating
from app.services import reviews as review_svc

router = APIRouter(prefix="/admin", tags=["reviews"], dependencies=[Depends(require_role("admin"))])

_CACHE_KEY = "cache:products:active"


class ModerateIn(BaseModel):
    status: str = Field(pattern="^(pending|approved|rejected|hidden)$")


class ReplyIn(BaseModel):
    reply: str = Field(min_length=1, max_length=1000)


class RatingAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    user_id: int
    stars: int
    comment: str | None = None
    status: str
    verified_purchase: bool
    helpful_count: int
    admin_reply: str | None = None


async def _locked(db: AsyncSession, rating_id: int) -> Rating:
    row = (
        await db.execute(select(Rating).where(Rating.id == rating_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    return row


@router.get("/ratings", response_model=list[RatingAdminOut], operation_id="admin_list_ratings")
async def list_ratings(
    status_filter: str | None = None,
    product_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Rating).order_by(Rating.id.desc())
    if status_filter in REVIEW_STATUSES:
        q = q.where(Rating.status == status_filter)
    if product_id is not None:
        q = q.where(Rating.product_id == product_id)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@router.post("/ratings/{rating_id}/moderate", response_model=RatingAdminOut, operation_id="moderate_rating")
async def moderate_rating(rating_id: int, data: ModerateIn, db: AsyncSession = Depends(get_db)):
    row = await _locked(db, rating_id)
    row.status = data.status
    await db.flush()
    # Hiding/rejecting a review changes the product's public aggregate.
    await review_svc.recompute_product_rating(db, row.product_id)
    await db.commit()
    await cache_delete(_CACHE_KEY)
    await db.refresh(row)
    return row


@router.post("/ratings/{rating_id}/reply", response_model=RatingAdminOut, operation_id="reply_rating")
async def reply_rating(rating_id: int, data: ReplyIn, db: AsyncSession = Depends(get_db)):
    row = await _locked(db, rating_id)
    row.admin_reply = data.reply
    row.replied_at = func.now()
    await db.commit()
    await db.refresh(row)
    return row
