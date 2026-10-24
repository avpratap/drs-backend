"""Admin read models for the dashboard: an account's wallet / credit ledgers and the
fulfilment record (POD, cash collected, returns) of a delivery order. Read-only."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.delivery import _order_detail
from app.core import media
from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, paged, pagination
from app.models.billing import CreditLedger, Wallet, WalletTransaction
from app.models.delivery import DeliveryOrder
from app.models.fulfillment import CashCollection, ProofOfDelivery, Return
from app.models.shop import CustomerAccount
from app.schemas.delivery import DeliveryOrderOut

router = APIRouter(prefix="/admin", tags=["admin-ops"], dependencies=[Depends(require_role("admin"))])


class AccountLedgerEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    entry_type: str
    amount: float
    balance_after: float
    ref_type: str | None = None
    ref_id: int | None = None
    created_at: dt.datetime


class PODOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    delivered_by: int | None = None
    recipient_name: str | None = None
    photo_url: str | None = None  # short-lived signed URL (private media)
    signature_url: str | None = None
    lat: float | None = None
    lng: float | None = None
    note: str | None = None
    captured_at: dt.datetime


class CashCollectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    amount_collected: float
    method: str
    reference: str | None = None
    collected_by: int | None = None
    collected_at: dt.datetime


class ReturnLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    qty_returned: float
    reason: str
    created_at: dt.datetime


class FulfillmentOut(BaseModel):
    order: DeliveryOrderOut
    pod: list[PODOut] = []
    cash: list[CashCollectionOut] = []
    returns: list[ReturnLineOut] = []
    cash_total: float = 0


async def _require_account(db: AsyncSession, account_id: int) -> None:
    if await db.get(CustomerAccount, account_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")


@router.get(
    "/accounts/{account_id}/wallet/transactions",
    response_model=list[AccountLedgerEntryOut],
    operation_id="admin_wallet_transactions",
)
async def wallet_transactions(
    account_id: int, db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)
):
    """The account's append-only wallet ledger, newest first (txn_type as entry_type)."""
    await _require_account(db, account_id)
    q = (
        select(WalletTransaction)
        .join(Wallet, Wallet.id == WalletTransaction.wallet_id)
        .where(Wallet.account_id == account_id)
        .order_by(WalletTransaction.id.desc())
    )
    rows = await paged(db, q, page)
    return [
        AccountLedgerEntryOut(
            id=t.id,
            entry_type=t.txn_type,
            amount=float(t.amount),
            balance_after=float(t.balance_after),
            ref_type=t.ref_type,
            ref_id=t.ref_id,
            created_at=t.created_at,
        )
        for t in rows
    ]


@router.get(
    "/accounts/{account_id}/credit/ledger",
    response_model=list[AccountLedgerEntryOut],
    operation_id="admin_credit_ledger",
)
async def credit_ledger(
    account_id: int, db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)
):
    """The account's postpaid receivables ledger (+charge, -payment/credit note), newest first."""
    await _require_account(db, account_id)
    q = select(CreditLedger).where(CreditLedger.account_id == account_id).order_by(CreditLedger.id.desc())
    return await paged(db, q, page)


@router.get(
    "/delivery-orders/{order_id}/fulfillment",
    response_model=FulfillmentOut,
    operation_id="admin_delivery_fulfillment",
)
async def delivery_fulfillment(order_id: int, db: AsyncSession = Depends(get_db)):
    """Everything captured at the door for one delivery order: proof of delivery (with
    signed, short-lived media URLs), cash/UPI collected and returned lines."""
    order = await db.get(DeliveryOrder, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Delivery order not found")
    pods = (
        await db.execute(
            select(ProofOfDelivery)
            .where(ProofOfDelivery.delivery_order_id == order_id)
            .order_by(ProofOfDelivery.id)
        )
    ).scalars().all()
    cash = (
        await db.execute(
            select(CashCollection)
            .where(CashCollection.delivery_order_id == order_id)
            .order_by(CashCollection.id)
        )
    ).scalars().all()
    returns = (
        await db.execute(select(Return).where(Return.delivery_order_id == order_id).order_by(Return.id))
    ).scalars().all()
    pod_out = []
    for p in pods:
        row = PODOut.model_validate(p)
        row.photo_url = media.resolve(p.photo_url)
        row.signature_url = media.resolve(p.signature_url)
        pod_out.append(row)
    cash_out = [CashCollectionOut.model_validate(c) for c in cash]
    return FulfillmentOut(
        order=await _order_detail(db, order),
        pod=pod_out,
        cash=cash_out,
        returns=[ReturnLineOut.model_validate(r) for r in returns],
        cash_total=round(sum(c.amount_collected for c in cash_out), 2),
    )
