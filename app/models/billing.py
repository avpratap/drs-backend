import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Wallet(Base, TimestampMixin):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), unique=True, nullable=False
    )
    balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)


class WalletTransaction(Base):
    """Append-only wallet ledger."""

    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True, nullable=False)
    txn_type: Mapped[str] = mapped_column(String(20), nullable=False)  # topup|debit_order|refund|adjustment
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)  # signed
    balance_after: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CreditLedger(Base):
    """Append-only postpaid (B2B) receivables ledger."""

    __tablename__ = "credit_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("customer_accounts.id"), index=True, nullable=False
    )
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)  # charge|payment|credit_note
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)  # signed (+charge, -payment)
    balance_after: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
