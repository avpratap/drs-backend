from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.models.billing import WalletTransaction
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.billing import BillingSummary, TopupIn, WalletTxnOut
from app.services import billing as billing_service

router = APIRouter(prefix="/billing", tags=["billing"])


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


@router.post("/admin/topup", dependencies=[Depends(require_role("admin"))], operation_id="wallet_topup")
async def topup(data: TopupIn, db: AsyncSession = Depends(get_db)):
    await billing_service.wallet_adjust(db, data.account_id, data.amount, "topup", "manual", None)
    await db.commit()
    return {"detail": "wallet topped up", "account_id": data.account_id, "amount": data.amount}
