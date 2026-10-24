import datetime as dt
import enum

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserRole(enum.StrEnum):
    admin = "admin"
    customer = "customer"
    subscriber = "subscriber"
    delivery = "delivery"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phone: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # Set once the user proves inbox ownership; cleared whenever the email changes.
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.customer.value, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
