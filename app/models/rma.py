"""Customer-initiated returns / RMA for one-time (e-commerce) orders.

Distinct from the delivery-side `Return` in app/models/fulfillment.py (which records
doorstep damage/refusal at a B2B delivery). A ReturnRequest is raised by the customer
against their Order, moves requested -> approved/rejected, and on refund credits the
customer's wallet as store credit.
"""

from sqlalchemy import BigInteger, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# requested -> approved -> refunded ; requested -> rejected ; requested -> cancelled
RMA_STATUSES = ("requested", "approved", "rejected", "refunded", "cancelled")


class ReturnRequest(Base, TimestampMixin):
    __tablename__ = "return_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rma_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="requested", nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(255), nullable=True)
    refund_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class ReturnRequestItem(Base):
    __tablename__ = "return_request_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    return_request_id: Mapped[int] = mapped_column(
        ForeignKey("return_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
