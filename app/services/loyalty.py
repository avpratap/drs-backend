"""Loyalty points, referral, gift-card logic. Balance rows locked FOR UPDATE;
every mutation appends an immutable ledger row; balances never go negative."""
import datetime as dt
import secrets

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.loyalty import (
    GiftCard,
    GiftCardTransaction,
    LoyaltyAccount,
    PointsLedger,
    ReferralCode,
    ReferralRedemption,
)
from app.services import billing

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous 0/O/1/I
_MAX_POINTS = 1_000_000_000  # keep within int4; reject absurd balances cleanly


def _token(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


async def _select_loyalty_locked(db: AsyncSession, account_id: int) -> LoyaltyAccount | None:
    return (
        await db.execute(
            select(LoyaltyAccount).where(LoyaltyAccount.account_id == account_id).with_for_update()
        )
    ).scalar_one_or_none()


async def _locked_loyalty(db: AsyncSession, account_id: int) -> LoyaltyAccount:
    row = await _select_loyalty_locked(db, account_id)
    if row is not None:
        return row
    # Create under a SAVEPOINT; a concurrent first-touch may win UNIQUE(account_id).
    # On conflict, re-select the now-existing row (locked) instead of 500ing.
    try:
        async with db.begin_nested():
            row = LoyaltyAccount(account_id=account_id, points_balance=0)
            db.add(row)
            await db.flush()
    except IntegrityError:
        row = await _select_loyalty_locked(db, account_id)
        if row is None:
            raise
        return row
    return (
        await db.execute(
            select(LoyaltyAccount).where(LoyaltyAccount.id == row.id).with_for_update()
        )
    ).scalar_one()


async def _move_points(
    db: AsyncSession, account_id: int, delta: int, reason: str, ref_type=None, ref_id=None
) -> LoyaltyAccount:
    la = await _locked_loyalty(db, account_id)
    new_balance = la.points_balance + delta
    if new_balance < 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Insufficient points balance")
    if new_balance > _MAX_POINTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "points balance too large")
    la.points_balance = new_balance
    db.add(
        PointsLedger(
            account_id=account_id, delta=delta, reason=reason,
            ref_type=ref_type, ref_id=ref_id, balance_after=new_balance,
        )
    )
    return la


async def earn_points(db: AsyncSession, account_id: int, points: int, reason: str = "earn", ref_type=None, ref_id=None) -> LoyaltyAccount:
    if points <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "points must be > 0")
    return await _move_points(db, account_id, points, reason, ref_type, ref_id)


async def redeem_points(db: AsyncSession, account_id: int, points: int, ref_type=None, ref_id=None) -> LoyaltyAccount:
    if points <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "points must be > 0")
    return await _move_points(db, account_id, -points, "redeem", ref_type, ref_id)


async def adjust_points(db: AsyncSession, account_id: int, points: int, reason: str = "adjust") -> LoyaltyAccount:
    """Admin signed adjustment (can be + or -, but never below zero)."""
    if points == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "points must be non-zero")
    return await _move_points(db, account_id, points, reason)


async def recent_ledger(db: AsyncSession, account_id: int, limit: int = 20):
    return (
        await db.execute(
            select(PointsLedger)
            .where(PointsLedger.account_id == account_id)
            .order_by(PointsLedger.id.desc())
            .limit(limit)
        )
    ).scalars().all()


async def get_or_create_referral_code(db: AsyncSession, account_id: int) -> ReferralCode:
    existing = await db.scalar(select(ReferralCode).where(ReferralCode.account_id == account_id))
    if existing is not None:
        return existing
    for _ in range(10):
        code = _token(8)
        if await db.scalar(select(ReferralCode.id).where(ReferralCode.code == code)) is None:
            break
    try:
        # Race-safe: a concurrent first call inserts for the same (unique) account_id.
        async with db.begin_nested():
            rc = ReferralCode(account_id=account_id, code=code)
            db.add(rc)
            await db.flush()
    except IntegrityError:
        rc = await db.scalar(select(ReferralCode).where(ReferralCode.account_id == account_id))
        if rc is None:  # the collision was on the random code itself — try once more
            return await get_or_create_referral_code(db, account_id)
    return rc


_REFERRAL_LOCK_NS = 556001


async def redeem_referral(db: AsyncSession, code: str, referee_account_id: int) -> ReferralCode:
    rc = await db.scalar(select(ReferralCode).where(ReferralCode.code == code))
    if rc is None or rc.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Referral code not found")
    if rc.account_id == referee_account_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot redeem your own referral code")
    # A referee is referred ONCE, by one code: redeeming many different codes (sock-puppet
    # farming of referral points) is refused. Serialized per referee.
    await db.execute(
        select(func.pg_advisory_xact_lock(_REFERRAL_LOCK_NS, referee_account_id & 0x7FFFFFFF))
    )
    prior = await db.scalar(
        select(ReferralRedemption.id).where(
            ReferralRedemption.referee_account_id == referee_account_id
        ).limit(1)
    )
    if prior is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Referral already redeemed")
    dup = await db.scalar(
        select(ReferralRedemption.id).where(
            ReferralRedemption.code_id == rc.id,
            ReferralRedemption.referee_account_id == referee_account_id,
        )
    )
    if dup is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Referral already redeemed")
    try:
        async with db.begin_nested():  # savepoint: concurrent redeem of the same code
            db.add(ReferralRedemption(code_id=rc.id, referee_account_id=referee_account_id))
            await db.flush()
    except IntegrityError:
        # uq(code_id, referee_account_id) — the other request won; no double-award.
        raise HTTPException(status.HTTP_409_CONFLICT, "Referral already redeemed") from None
    # A referee who has already bought something qualifies right away; otherwise the
    # reward waits for their first paid order (reward_referral_on_purchase).
    await reward_referral_on_purchase(db, referee_account_id)
    return rc


async def reward_referral_on_purchase(db: AsyncSession, referee_account_id: int) -> bool:
    """Pay the referrer once the referee has a paid order. Idempotent (rewarded_at under
    a row lock); called on redeem and whenever an order becomes paid. Flush-only."""
    from app.models.order import Order

    red = (
        await db.execute(
            select(ReferralRedemption)
            .where(
                ReferralRedemption.referee_account_id == referee_account_id,
                ReferralRedemption.rewarded_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().first()
    if red is None:
        return False
    paid = await db.scalar(
        select(func.count()).select_from(Order).where(
            Order.account_id == referee_account_id,
            Order.payment_status.in_(("paid", "partially_refunded", "refunded")),
            Order.status != "cancelled",
        )
    )
    if not paid:
        return False
    rc = await db.get(ReferralCode, red.code_id)
    if rc is None:
        return False
    red.rewarded_at = dt.datetime.now(dt.UTC)
    await earn_points(db, rc.account_id, rc.reward_points, "referral", "referral_code", rc.id)
    return True


async def issue_gift_card(db: AsyncSession, amount: float, issued_to=None, expires_at: dt.date | None = None) -> GiftCard:
    if amount <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "amount must be > 0")
    if expires_at is not None and expires_at < dt.date.today():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expires_at is in the past")
    for _ in range(10):
        code = _token(16)
        if await db.scalar(select(GiftCard.id).where(GiftCard.code == code)) is None:
            break
    amount = round(float(amount), 2)
    gc = GiftCard(
        code=code, initial_amount=amount, balance=amount,
        status="active", issued_to_account_id=issued_to, expires_at=expires_at,
    )
    db.add(gc)
    await db.flush()
    db.add(GiftCardTransaction(gift_card_id=gc.id, delta=amount, reason="issue", balance_after=amount))
    return gc


async def redeem_gift_card(db: AsyncSession, code: str, account_id: int, amount: float) -> GiftCard:
    gc = (
        await db.execute(select(GiftCard).where(GiftCard.code == code).with_for_update())
    ).scalar_one_or_none()
    if gc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gift card not found")
    # If the card was issued to a specific account, only that account may redeem it.
    # 404 (not 403) so a card's existence isn't enumerable by a non-recipient.
    if gc.issued_to_account_id is not None and gc.issued_to_account_id != account_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gift card not found")
    if gc.status in ("disabled", "redeemed", "expired"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Gift card is {gc.status}")
    if gc.expires_at is not None and gc.expires_at < dt.date.today():
        # (Status is flipped to 'expired' by the expiry sweep, not here — a raise would
        # roll back the write anyway; the check alone blocks redemption.)
        raise HTTPException(status.HTTP_409_CONFLICT, "Gift card has expired")
    amount = round(float(amount), 2)
    if amount <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "amount must be > 0")
    if amount > float(gc.balance):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Amount exceeds gift-card balance")
    new_balance = round(float(gc.balance) - amount, 2)
    gc.balance = new_balance
    if new_balance == 0:
        gc.status = "redeemed"
    gct = GiftCardTransaction(
        gift_card_id=gc.id, delta=-amount, reason="redeem",
        ref_type="account", ref_id=account_id, balance_after=new_balance,
    )
    db.add(gct)
    await db.flush()
    # The redeemed value goes to the customer's wallet (store credit) in the SAME
    # transaction — previously the card was debited and the customer received nothing.
    await billing.wallet_adjust(db, account_id, amount, "giftcard", "gift_card_txn", gct.id)
    return gc
