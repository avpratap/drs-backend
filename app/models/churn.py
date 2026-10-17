"""Churn-risk scores per customer account (refreshed by a nightly ML job).

Scores are derived on our own infra from aggregate order/subscription/payment
signals — no PII leaves the box (DPDP). One current row per account.
"""
import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChurnScore(Base):
    __tablename__ = "churn_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), unique=True, index=True, nullable=False
    )
    score: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)  # 0.000 .. 1.000
    risk: Mapped[str] = mapped_column(String(8), nullable=False)  # low|medium|high
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
