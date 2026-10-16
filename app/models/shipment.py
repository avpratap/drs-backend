"""Courier shipments for one-time (e-commerce) orders.

Distinct from the in-house daily DeliveryOrder: a Shipment is a parcel handed to a
courier (Shiprocket) with an AWB and tracking events. One order can have shipments;
tracking updates arrive via webhook and append ShipmentTrackingEvent rows.
"""
import datetime as dt

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# draft -> created (AWB) -> in_transit -> out_for_delivery -> delivered
#                                                        \-> rto ; or cancelled / failed
SHIPMENT_STATUSES = (
    "draft", "created", "in_transit", "out_for_delivery",
    "delivered", "rto", "cancelled", "failed",
)


class Shipment(Base, TimestampMixin):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shipment_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    courier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    awb: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    tracking_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    label_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    shipping_cost: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    cod_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    weight_kg: Mapped[float] = mapped_column(Numeric(8, 3), default=0.5, nullable=False)
    # Destination snapshot (so a later address edit doesn't rewrite shipped parcels).
    to_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    to_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    to_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    to_pincode: Mapped[str] = mapped_column(String(10), nullable=False)
    gateway_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway_shipment_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)


class ShipmentTrackingEvent(Base):
    __tablename__ = "shipment_tracking_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    event_time: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
