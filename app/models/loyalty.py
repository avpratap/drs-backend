"""Loyalty points, referral program, and gift cards.

Points and gift-card balances are money-like: the balance row is locked FOR UPDATE
before a read-modify-write, and every change appends an immutable ledger row
(points_ledger / gift_card_transactions are append-only, like wallet_transactions).
"""
import datetime as dt

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LoyaltyAccount(Base):
    __tablename__ = "loyalty_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), unique=True, nullable=False
    )
    points_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PointsLedger(Base):
    """Append-only points ledger."""

    __tablename__ = "points_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)  # signed
    reason: Mapped[str] = mapped_column(String(16), nullable=False)  # earn|redeem|adjust|referral
    ref_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReferralCode(Base):
    __tablename__ = "referral_codes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), unique=True, nullable=False
    )
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    reward_points: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    referee_discount: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)  # active|disabled
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReferralRedemption(Base):
    __tablename__ = "referral_redemptions"
    __table_args__ = (
        UniqueConstraint("code_id", "referee_account_id", name="uq_referral_once_per_referee"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code_id: Mapped[int] = mapped_column(
        ForeignKey("referral_codes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    referee_account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GiftCard(Base):
    __tablename__ = "gift_cards"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    initial_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    balance: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), default="active", nullable=False
    )  # active|redeemed|expired|disabled
    issued_to_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=True
    )
    expires_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GiftCardTransaction(Base):
    """Append-only gift-card ledger."""

    __tablename__ = "gift_card_transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    gift_card_id: Mapped[int] = mapped_column(
        ForeignKey("gift_cards.id"), index=True, nullable=False
    )
    delta: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)  # signed
    reason: Mapped[str] = mapped_column(String(10), nullable=False)  # issue|redeem|adjust
    ref_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    balance_after: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
