"""Stock-holding locations: plants, distribution hubs, cold storage, vehicles, stores."""
import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Location(Base):
    __tablename__ = "locations"
    # Names are unique ignoring case (migration 0057); the API trims + returns 409.
    __table_args__ = (Index("uq_locations_name_ci", text("lower(name)"), unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), default="warehouse", nullable=False
    )  # plant|hub|cold_storage|vehicle|store|warehouse
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    # A 'vehicle' location carried by this delivery driver: deliveries they make deduct
    # stock from the van (not the warehouse, which a transfer already depleted).
    driver_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
