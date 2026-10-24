"""User-facing identity & commerce profile: profile, address book, saved payment
methods, autopay mandates, and an append-only DPDP consent ledger.

Design notes
------------
- Profile is 1:1 with users (PK == user_id); channel toggles live here, legal
  consent lives in the append-only Consent ledger (never mutated, only appended).
- Addresses & payment methods belong to a CustomerAccount (the billing entity),
  so every shop/user under that account shares them. Soft-deleted via `status`.
- At most one default address and one default payment method per account is
  enforced by a partial unique index (see migration 0011), not in Python.
- We NEVER store PAN/CVV/UPI credentials — only the gateway's vault token and a
  masked display string. PCI-DSS scope stays with Razorpay.
"""
import datetime as dt

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserProfile(Base, TimestampMixin):
    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    date_of_birth: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(16), nullable=True)
    locale: Mapped[str] = mapped_column(String(12), default="en-IN", nullable=False)
    alternate_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # GST / business identity (B2B invoicing)
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Notification channel toggles (per-category legal consent is in Consent)
    notify_whatsapp: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )  # on only with a whatsapp_optin (Meta policy)
    notify_sms: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_push: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Quiet hours (local, IST) — suppress non-critical pushes/WA in this window
    quiet_hours_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quiet_hours_end: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Address(Base, TimestampMixin):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(40), nullable=True)  # Home/Work/Shop
    contact_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    line1: Mapped[str] = mapped_column(String(255), nullable=False)
    line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    landmark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(120), nullable=False)
    pincode: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(2), default="IN", nullable=False)
    geo: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )  # active|archived


class PaymentMethod(Base, TimestampMixin):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    method_type: Mapped[str] = mapped_column(String(16), nullable=False)  # upi|card|netbanking
    gateway: Mapped[str] = mapped_column(String(16), default="razorpay", nullable=False)
    gateway_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway_token: Mapped[str | None] = mapped_column(String(128), nullable=True)  # vault token
    display: Mapped[str | None] = mapped_column(String(64), nullable=True)  # •••• 4242 / vpa@bank
    brand: Mapped[str | None] = mapped_column(String(32), nullable=True)  # Visa / GPay / HDFC
    expiry_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expiry_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )  # active|removed


class PaymentMandate(Base, TimestampMixin):
    """UPI AutoPay / e-mandate authorizing recurring debits for subscriptions."""

    __tablename__ = "payment_mandates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id"), nullable=True
    )
    gateway: Mapped[str] = mapped_column(String(16), default="razorpay", nullable=False)
    gateway_mandate_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )
    max_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)  # per-debit cap (₹)
    frequency: Mapped[str] = mapped_column(
        String(16), default="as_presented", nullable=False
    )  # daily|weekly|monthly|as_presented
    valid_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="created", nullable=False
    )  # created|active|paused|cancelled|expired


class Consent(Base):
    """Append-only DPDP consent ledger. Rows are never updated or deleted."""

    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # whatsapp_optin|marketing_sms|marketing_email|marketing_push|marketing_whatsapp|data_processing
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # granted|withdrawn
    source: Mapped[str] = mapped_column(String(16), default="app", nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
