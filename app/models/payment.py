import datetime as dt

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    payment_type: Mapped[str] = mapped_column(String(20), nullable=False)  # wallet_topup|order_payment
    gateway: Mapped[str] = mapped_column(String(16), default="razorpay", nullable=False)
    gateway_order_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="initiated", nullable=False)  # creating|initiated|success|failed
    ref_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # An order capture that did NOT settle its order (another payment already had, or the
    # order was not payable): money to hand back, never the order's own payment. Its
    # refunds don't touch the order's status/credit notes or the order's refund cap.
    is_excess: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
