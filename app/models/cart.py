"""Shopping cart for one-time (e-commerce) checkout.

At most one 'active' cart per account (partial unique index in the migration).
Checkout converts the active cart into an Order and marks the cart 'converted'.
"""
from sqlalchemy import BigInteger, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Cart(Base, TimestampMixin):
    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )  # active|converted|abandoned


class CartItem(Base):
    __tablename__ = "cart_items"
    # One line per (product, pack) so a 1 kg pack and a 0.5 kg pack of the same product are
    # distinct lines the cart can label and step independently.
    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", "pack_size", name="uq_cart_items_cart_product_pack"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cart_id: Mapped[int] = mapped_column(
        ForeignKey("carts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    # The buyable pack the customer chose (base units per pack), e.g. 0.5 or 1 kg. qty is a
    # whole number of these. Lets the cart show the right pack and step by it.
    pack_size: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
