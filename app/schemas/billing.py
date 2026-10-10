from pydantic import BaseModel, ConfigDict


class WalletTxnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    txn_type: str
    amount: float
    balance_after: float


class BillingSummary(BaseModel):
    account_id: int
    billing_mode: str
    wallet_balance: float
    credit_outstanding: float
    recent_wallet_txns: list[WalletTxnOut] = []


class TopupIn(BaseModel):
    account_id: int
    amount: float
