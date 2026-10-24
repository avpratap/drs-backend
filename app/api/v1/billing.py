from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.idempotency import require_idempotency_key
from app.models.billing import WalletTransaction, WithdrawalRequest
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.billing import BillingSummary, TopupIn, WalletTxnOut
from app.services import admin_tools as audit_svc
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
    dependencies=[Depends(require_role("admin")), Depends(require_idempotency_key)],
    operation_id="wallet_topup",
    response_model=TopupOut,
)
async def topup(
    data: TopupIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    await billing_service.wallet_adjust(db, data.account_id, data.amount, "topup", "manual", None)
    await audit_svc.record_audit(
        db, user.id, "wallet_topup", "customer_account", data.account_id,
        detail={"amount": float(data.amount)},
    )
    await db.commit()
    return {"detail": "wallet topped up", "account_id": data.account_id, "amount": data.amount}


@router.post("/withdrawals", response_model=WithdrawalOut, operation_id="request_withdrawal")
async def request_withdrawal(
    data: WithdrawalIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Customer requests a wallet payout (must have the balance; admin approves)."""
    account = await _my_account(db, user)
    # Lock the wallet so two concurrent requests can't both pass the available-balance
    # check (they serialize; the second sees the first's 'requested' reservation).
    wallet = await billing_service.get_or_create_wallet(db, account.id, lock=True)
    # Reserve against already-pending requests so a customer can't open several requests
    # that together exceed the balance (available = balance - sum of 'requested').
    pending = float(
        await db.scalar(
            select(func.coalesce(func.sum(WithdrawalRequest.amount), 0)).where(
                WithdrawalRequest.account_id == account.id,
                WithdrawalRequest.status == "requested",
            )
        )
        or 0
    )
    cash = await billing_service.withdrawable_cash(db, account.id, float(wallet.balance))
    if data.amount > round(cash - pending, 2):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Amount exceeds the withdrawable wallet balance (cash paid in, after pending "
            "withdrawals; gift-card and store credit can't be withdrawn)",
        )
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
        .limit(500)  # bounded: a single account's withdrawals
    )
    return res.scalars().all()
