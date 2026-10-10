"""Wallet (prepaid) + credit (postpaid) ledgers. Ledgers are append-only.

Concurrency-safe: wallet/account rows are locked FOR UPDATE before a
read-modify-write, so concurrent debits/credits can't lose updates. Billing for
a delivery is idempotent (one ledger entry per delivery_order).
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import CreditLedger, Wallet, WalletTransaction
from app.models.delivery import DeliveryOrder
from app.models.shop import CustomerAccount


class InsufficientBalance(Exception):
    pass


async def get_or_create_wallet(db: AsyncSession, account_id: int, *, lock: bool = False) -> Wallet:
    query = select(Wallet).where(Wallet.account_id == account_id)
    if lock:
        query = query.with_for_update()
    res = await db.execute(query)
    wallet = res.scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(account_id=account_id, balance=0)
        db.add(wallet)
        await db.flush()
        if lock:  # re-select with the lock held now that the row exists
            res = await db.execute(
                select(Wallet).where(Wallet.id == wallet.id).with_for_update()
            )
            wallet = res.scalar_one()
    return wallet


async def wallet_adjust(
    db: AsyncSession,
    account_id: int,
    amount: float,
    txn_type: str,
    ref_type=None,
    ref_id=None,
    *,
    allow_negative: bool = False,
) -> Wallet:
    wallet = await get_or_create_wallet(db, account_id, lock=True)  # row-locked
    new_balance = round(float(wallet.balance) + amount, 2)
    if amount < 0 and new_balance < 0 and not allow_negative:
        raise InsufficientBalance("insufficient_wallet_balance")
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
    # Serialize concurrent credit entries for this account by locking the account row.
    await db.execute(
        select(CustomerAccount.id).where(CustomerAccount.id == account_id).with_for_update()
    )
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


async def _already_billed(db: AsyncSession, account: CustomerAccount, order_id: int) -> bool:
    if account.billing_mode == "postpaid_credit":
        res = await db.execute(
            select(func.count())
            .select_from(CreditLedger)
            .where(CreditLedger.ref_type == "delivery_order", CreditLedger.ref_id == order_id)
        )
    else:
        res = await db.execute(
            select(func.count())
            .select_from(WalletTransaction)
            .where(WalletTransaction.ref_type == "delivery_order", WalletTransaction.ref_id == order_id)
        )
    return (res.scalar_one() or 0) > 0


async def bill_delivery(db: AsyncSession, order: DeliveryOrder, amount: float) -> None:
    """Charge for a delivered order: wallet debit (prepaid) or credit charge (postpaid).
    Idempotent — at most one ledger entry per delivery_order."""
    if amount <= 0:
        return
    account = await db.get(CustomerAccount, order.account_id)
    if account is None:
        await wallet_adjust(
            db, order.account_id, -amount, "debit_order", "delivery_order", order.id,
            allow_negative=True,
        )
        return
    if await _already_billed(db, account, order.id):
        return  # already charged for this delivery
    if account.billing_mode == "postpaid_credit":
        await credit_entry(db, account.id, amount, "charge", "delivery_order", order.id)
    else:
        await wallet_adjust(
            db, order.account_id, -amount, "debit_order", "delivery_order", order.id,
            allow_negative=True,
        )
