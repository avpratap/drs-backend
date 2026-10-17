"""Driver live-location pings for the delivery app.

Append-only: each GPS ping inserts a row; the latest row per driver is their
current position (used for route ordering / live tracking).
"""
import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DriverLocation(Base):
    __tablename__ = "driver_locations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    lat: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    lng: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
