from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.models.billing import WalletTransaction, WithdrawalRequest
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.billing import BillingSummary, TopupIn, WalletTxnOut
from app.services import billing as billing_service

router = APIRouter(prefix="/billing", tags=["billing"])


class WithdrawalIn(BaseModel):
    amount: float = Field(gt=0, le=10_000_000, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=255)


class TopupOut(BaseModel):
    detail: str
    account_id: int
    amount: float


class WithdrawalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    amount: float
    status: str
    note: str | None = None


async def _my_account(db: AsyncSession, user: User) -> CustomerAccount:
    account = await db.scalar(
        select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id)
    )
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No billing account for this user")
    return account


@router.get("/me", response_model=BillingSummary, operation_id="my_billing")
async def my_billing(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id))
    account = res.scalar_one_or_none()
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No billing account for this user")

    wallet = await billing_service.get_or_create_wallet(db, account.id)
    await db.commit()

    res = await db.execute(
        select(WalletTransaction)
        .where(WalletTransaction.wallet_id == wallet.id)
        .order_by(WalletTransaction.id.desc())
        .limit(5)
    )
    txns = [WalletTxnOut.model_validate(t) for t in res.scalars().all()]
    outstanding = await billing_service.credit_outstanding(db, account.id)
    return BillingSummary(
        account_id=account.id,
        billing_mode=account.billing_mode,
        wallet_balance=float(wallet.balance),
        credit_outstanding=outstanding,
        recent_wallet_txns=txns,
    )


@router.post(
    "/admin/topup",
    dependencies=[Depends(require_role("admin"))],
    operation_id="wallet_topup",
    response_model=TopupOut,
)
async def topup(data: TopupIn, db: AsyncSession = Depends(get_db)):
    await billing_service.wallet_adjust(db, data.account_id, data.amount, "topup", "manual", None)
    await db.commit()
    return {"detail": "wallet topped up", "account_id": data.account_id, "amount": data.amount}


@router.post("/withdrawals", response_model=WithdrawalOut, operation_id="request_withdrawal")
async def request_withdrawal(
    data: WithdrawalIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Customer requests a wallet payout (must have the balance; admin approves)."""
    account = await _my_account(db, user)
    wallet = await billing_service.get_or_create_wallet(db, account.id)
    if data.amount > float(wallet.balance):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Amount exceeds wallet balance")
    wd = WithdrawalRequest(account_id=account.id, amount=data.amount, note=data.note)
    db.add(wd)
    await db.commit()
    await db.refresh(wd)
    return wd


@router.get("/withdrawals", response_model=list[WithdrawalOut], operation_id="my_withdrawals")
async def my_withdrawals(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    account = await _my_account(db, user)
    res = await db.execute(
        select(WithdrawalRequest)
        .where(WithdrawalRequest.account_id == account.id)
        .order_by(WithdrawalRequest.id.desc())
    )
    return res.scalars().all()
