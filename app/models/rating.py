"""Product ratings/reviews (one per user per product, updatable) + moderation."""
import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# pending|approved|rejected|hidden — default 'approved' keeps pre-moderation behaviour.
REVIEW_STATUSES = ("pending", "approved", "rejected", "hidden")


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_rating_user_product"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..5
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="approved", nullable=False)
    verified_purchase: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    helpful_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    admin_reply: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    replied_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReviewHelpful(Base):
    """One 'helpful' vote per user per review."""

    __tablename__ = "review_helpful"
    __table_args__ = (UniqueConstraint("rating_id", "user_id", name="uq_review_helpful_user"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rating_id: Mapped[int] = mapped_column(
        ForeignKey("ratings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
