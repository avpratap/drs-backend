import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ProofOfDelivery(Base):
    __tablename__ = "proof_of_delivery"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_order_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_orders.id"), index=True, nullable=False
    )
    delivered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    recipient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signature_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lat: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    lng: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Return(Base):
    __tablename__ = "returns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_order_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_orders.id"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty_returned: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)  # damaged|expired|wrong_item|excess|refused
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CashCollection(Base):
    __tablename__ = "cash_collections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_order_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_orders.id"), index=True, nullable=False
    )
    amount_collected: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[str] = mapped_column(String(16), default="cash", nullable=False)  # cash|upi|cheque
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    collected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    collected_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
