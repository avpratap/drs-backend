import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class WalletTxnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    txn_type: str
    amount: float
    balance_after: float


class WalletTxnLineOut(BaseModel):
    """One line of the customer's wallet ledger (append-only)."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    txn_type: str
    amount: float  # signed: + credit, - debit
    balance_after: float
    ref_type: str | None = None
    ref_id: int | None = None
    created_at: dt.datetime


class CreditLedgerLineOut(BaseModel):
    """One line of a B2B credit-terms statement (append-only receivables ledger)."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    entry_type: str  # charge | payment | credit_note
    amount: float  # signed: + charge (owed more), - payment/credit
    balance_after: float  # outstanding after this entry
    ref_type: str | None = None
    ref_id: int | None = None
    created_at: dt.datetime


class BillingSummary(BaseModel):
    account_id: int
    billing_mode: str
    wallet_balance: float
    credit_outstanding: float
    recent_wallet_txns: list[WalletTxnOut] = []


class TopupIn(BaseModel):
    account_id: int
    amount: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
