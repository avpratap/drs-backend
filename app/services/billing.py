"""Wallet (prepaid) + credit (postpaid) ledgers. Ledgers are append-only.

Concurrency-safe: wallet/account rows are locked FOR UPDATE before a
read-modify-write, so concurrent debits/credits can't lose updates. Billing for
a delivery is idempotent (one ledger entry per delivery_order).
"""
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.billing import CreditLedger, Wallet, WalletTransaction
from app.models.delivery import DeliveryOrder
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import notifications as notify


class InsufficientBalance(Exception):
    pass


class InsufficientPrepaidBalance(Exception):
    """A prepaid wallet can't cover a delivery: the delivery must be skipped/deferred,
    never billed into the negative."""

    def __init__(self, balance: float, needed: float):
        super().__init__(f"prepaid balance {balance:.2f} < {needed:.2f}")
        self.balance = balance
        self.needed = needed


async def get_or_create_wallet(db: AsyncSession, account_id: int, *, lock: bool = False) -> Wallet:
    query = select(Wallet).where(Wallet.account_id == account_id)
    if lock:
        query = query.with_for_update()
    res = await db.execute(query)
    wallet = res.scalar_one_or_none()
    if wallet is None:
        # Two concurrent first-touches would both insert and collide on
        # UNIQUE(account_id). Create in a savepoint; on collision, re-select the row
        # the other transaction committed (locked if requested) instead of 500ing.
        try:
            async with db.begin_nested():
                wallet = Wallet(account_id=account_id, balance=0)
                db.add(wallet)
                await db.flush()
        except IntegrityError:
            wallet = None
        q2 = select(Wallet).where(Wallet.account_id == account_id)
        if lock:
            q2 = q2.with_for_update()
        wallet = (await db.execute(q2)).scalar_one()
    return wallet


# Ledger entries that move CASH in or out of the wallet. Gift-card redemptions, RMA store
# credit and admin goodwill are wallet credit too, but were never paid in as money, so
# they can be spent on deliveries but never paid out as cash. Spending is assumed to use
# cash FIRST (debit_order counts against it; a door cash_collection that pays a delivery
# offsets its own debit), so credit can't be laundered into cash by spending the cash.
_CASH_TXN_TYPES = (
    "topup", "refund_reversal", "refund_release", "withdrawal", "debit_order", "cash_collection",
)


async def withdrawable_cash(db: AsyncSession, account_id: int, balance: float) -> float:
    """Cash the account may withdraw: its net cash paid in, never above the balance."""
    cash = float(
        await db.scalar(
            select(func.coalesce(func.sum(WalletTransaction.amount), 0))
            .join(Wallet, Wallet.id == WalletTransaction.wallet_id)
            .where(
                Wallet.account_id == account_id,
                WalletTransaction.txn_type.in_(_CASH_TXN_TYPES),
            )
        )
        or 0
    )
    return max(round(min(cash, float(balance)), 2), 0.0)


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
    # Quantize ONCE so the stored txn amount and the balance move are the same paisa
    # value (Numeric(12,2) would otherwise round 0.125 -> 0.13 while balance moved 0.12).
    amount = round(float(amount), 2)
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
    # Quantize ONCE (as wallet_adjust does) so the stored entry and the balance move are
    # the same paisa value: storing the raw amount while rounding balance_after let the
    # append-only ledger's lines stop summing to its balance on sub-paisa input.
    amount = round(float(amount), 2)
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
    # Check BOTH ledgers regardless of the account's CURRENT billing_mode: if the mode
    # changed between two deliveries of the same order, a mode-specific check would miss
    # the entry made under the old mode and double-bill. A charge in EITHER ledger counts.
    credit = await db.scalar(
        select(func.count())
        .select_from(CreditLedger)
        .where(CreditLedger.ref_type == "delivery_order", CreditLedger.ref_id == order_id)
    )
    if (credit or 0) > 0:
        return True
    wallet = await db.scalar(
        select(func.count())
        .select_from(WalletTransaction)
        .where(
            WalletTransaction.ref_type == "delivery_order",
            WalletTransaction.ref_id == order_id,
        )
    )
    return (wallet or 0) > 0


async def bill_delivery(db: AsyncSession, order: DeliveryOrder, amount: float) -> None:
    """Charge for a delivered order: wallet debit (prepaid) or credit charge (postpaid).
    Idempotent — at most one ledger entry per delivery_order."""
    if amount <= 0:
        return
    account = await db.get(CustomerAccount, order.account_id)
    if account is None:
        # No account row (orphan order): still idempotent — one wallet debit per order.
        existing = await db.scalar(
            select(func.count())
            .select_from(WalletTransaction)
            .where(
                WalletTransaction.ref_type == "delivery_order",
                WalletTransaction.ref_id == order.id,
            )
        )
        if not existing:
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
        # Prepaid invariant (retail AND B2B): the wallet never goes negative for a
        # delivery. Checked under the wallet row lock — the cutoff's funds gate is only a
        # start-of-run snapshot, so spending/other deliveries since then can't overdraw.
        wallet = await get_or_create_wallet(db, account.id, lock=True)
        if round(float(wallet.balance), 2) < round(float(amount), 2):
            raise InsufficientPrepaidBalance(float(wallet.balance), round(float(amount), 2))
        wallet = await wallet_adjust(
            db, order.account_id, -amount, "debit_order", "delivery_order", order.id,
        )
        if float(wallet.balance) < settings.WALLET_LOW_THRESHOLD:
            owner = await db.get(User, account.owner_user_id)
            if owner is not None:
                await notify.emit_event(
                    db,
                    "wallet.low",
                    owner,
                    {"balance": float(wallet.balance), "delivery_id": order.id},
                    account_id=account.id,
                    dedupe_key=f"wallet.low:{order.id}",
                )
