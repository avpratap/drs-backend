"""Wallet (prepaid) + credit (postpaid) ledgers. Ledgers are append-only."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import CreditLedger, Wallet, WalletTransaction
from app.models.delivery import DeliveryOrder
from app.models.shop import CustomerAccount


async def get_or_create_wallet(db: AsyncSession, account_id: int) -> Wallet:
    res = await db.execute(select(Wallet).where(Wallet.account_id == account_id))
    wallet = res.scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(account_id=account_id, balance=0)
        db.add(wallet)
        await db.flush()
    return wallet


async def wallet_adjust(
    db: AsyncSession, account_id: int, amount: float, txn_type: str, ref_type=None, ref_id=None
) -> Wallet:
    wallet = await get_or_create_wallet(db, account_id)
    new_balance = round(float(wallet.balance) + amount, 2)
    wallet.balance = new_balance
    db.add(
        WalletTransaction(
            wallet_id=wallet.id,
            txn_type=txn_type,
            amount=amount,
            balance_after=new_balance,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    )
    return wallet


async def credit_outstanding(db: AsyncSession, account_id: int) -> float:
    res = await db.execute(
        select(CreditLedger)
        .where(CreditLedger.account_id == account_id)
        .order_by(CreditLedger.id.desc())
        .limit(1)
    )
    last = res.scalar_one_or_none()
    return float(last.balance_after) if last else 0.0


async def credit_entry(
    db: AsyncSession, account_id: int, amount: float, entry_type: str, ref_type=None, ref_id=None
) -> float:
    new_balance = round(await credit_outstanding(db, account_id) + amount, 2)
    db.add(
        CreditLedger(
            account_id=account_id,
            entry_type=entry_type,
            amount=amount,
            balance_after=new_balance,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    )
    return new_balance


async def bill_delivery(db: AsyncSession, order: DeliveryOrder, amount: float) -> None:
    """Charge for a delivered order: wallet debit (prepaid) or credit charge (postpaid)."""
    if amount <= 0:
        return
    account = await db.get(CustomerAccount, order.account_id)
    if account is not None and account.billing_mode == "postpaid_credit":
        await credit_entry(db, account.id, amount, "charge", "delivery_order", order.id)
    else:
        await wallet_adjust(db, order.account_id, -amount, "debit_order", "delivery_order", order.id)
