"""Ledger integrity: credit entries store the same rounded paisa amount that moved the
balance, and the DB refuses a second ledger row for a one-per-reference money event."""
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.db import SessionLocal
from app.models.billing import CreditLedger, Wallet, WalletTransaction
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import billing


async def _account(db) -> int:
    user = User(phone=f"6{uuid.uuid4().int % 10**9:09d}", role="subscriber", status="active")
    db.add(user)
    await db.flush()
    acct = CustomerAccount(owner_user_id=user.id, account_type="b2b", billing_mode="postpaid_credit")
    db.add(acct)
    await db.flush()
    return acct.id


async def test_credit_entry_stores_the_rounded_amount():
    async with SessionLocal() as db:
        acct = await _account(db)
        for amt in (10.005, 0.333, 7.125, -3.336):
            await billing.credit_entry(db, acct, amt, "charge", "test", None)
        rows = (await db.execute(
            select(CreditLedger).where(CreditLedger.account_id == acct).order_by(CreditLedger.id)
        )).scalars().all()
        running = 0.0
        for r in rows:
            assert float(r.amount) == round(float(r.amount), 2)
            running = round(running + float(r.amount), 2)
            assert float(r.balance_after) == running  # lines sum exactly to the balance
        await db.rollback()


@pytest.mark.parametrize(
    "first,second",
    [
        (("debit_order", "delivery_order"), ("debit_order", "delivery_order")),
        (("cash_collection", "cash_collection"), ("cash_collection", "cash_collection")),
        (("topup", "payment"), ("topup", "payment")),
    ],
)
async def test_db_refuses_a_second_wallet_row_for_the_same_event(first, second):
    async with SessionLocal() as db:
        acct = await _account(db)
        wallet = Wallet(account_id=acct, balance=0)
        db.add(wallet)
        await db.flush()
        ref = 900_000_000 + uuid.uuid4().int % 10**8
        db.add(WalletTransaction(wallet_id=wallet.id, txn_type=first[0], amount=-1,
                                 balance_after=-1, ref_type=first[1], ref_id=ref))
        await db.flush()
        db.add(WalletTransaction(wallet_id=wallet.id, txn_type=second[0], amount=-1,
                                 balance_after=-2, ref_type=second[1], ref_id=ref))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_db_refuses_a_second_credit_charge_but_allows_other_types():
    async with SessionLocal() as db:
        acct = await _account(db)
        ref = 900_000_000 + uuid.uuid4().int % 10**8
        await billing.credit_entry(db, acct, 100, "charge", "delivery_order", ref)
        # A different entry type on the same reference is fine (e.g. a credit note).
        await billing.credit_entry(db, acct, -20, "credit_note", "delivery_order", ref)
        # Manual entries (no ref) never collide.
        await billing.credit_entry(db, acct, 5, "charge", "manual", None)
        await billing.credit_entry(db, acct, 5, "charge", "manual", None)
        await db.flush()
        n = await db.scalar(select(func.count()).select_from(CreditLedger).where(
            CreditLedger.account_id == acct))
        assert n == 4
        await billing.credit_entry(db, acct, 100, "charge", "delivery_order", ref)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
