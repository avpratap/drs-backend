"""Loyalty points, referral program, and gift cards (customer + admin)."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, pagination
from app.models.loyalty import GiftCard, LoyaltyAccount
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import loyalty

router = APIRouter(tags=["loyalty"])


# ---------- Schemas ----------
class LedgerEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    delta: int
    reason: str
    balance_after: int


class LoyaltyOut(BaseModel):
    account_id: int
    points_balance: int
    recent: list[LedgerEntryOut] = []


class ReferralCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    reward_points: int
    status: str


class RedeemReferralIn(BaseModel):
    code: str = Field(min_length=4, max_length=16)


class ReferralRedeemedOut(BaseModel):
    detail: str
    code: str


class GiftCardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    initial_amount: float
    balance: float
    status: str
    expires_at: dt.date | None = None


class RedeemGiftIn(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    amount: float = Field(gt=0, le=10_000_000, allow_inf_nan=False)


class AdjustPointsIn(BaseModel):
    points: int  # non-zero enforced in the service (signed: + or -)
    reason: str = Field(default="adjust", max_length=16)


class IssueGiftIn(BaseModel):
    amount: float = Field(gt=0, le=10_000_000, allow_inf_nan=False)
    issued_to_account_id: int | None = None
    expires_at: dt.date | None = None


class LoyaltyAccountAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    account_id: int
    points_balance: int


class GiftCardAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    initial_amount: float
    balance: float
    status: str
    issued_to_account_id: int | None = None
    expires_at: dt.date | None = None


async def _my_account(db: AsyncSession, user: User) -> CustomerAccount:
    """Resolve the caller's account, auto-creating a retail one (mirrors orders)."""
    acct = await db.scalar(select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id))
    if acct is None:
        # Race-safe create (a concurrent first call may win uq_customer_accounts_owner).
        try:
            async with db.begin_nested():
                acct = CustomerAccount(
                    owner_user_id=user.id, account_type="retail", billing_mode="prepaid_wallet"
                )
                db.add(acct)
                await db.flush()
        except IntegrityError:
            acct = (
                await db.execute(
                    select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id)
                )
            ).scalar_one()
    return acct


# ---------- Customer: points ----------
@router.get("/loyalty", response_model=LoyaltyOut, operation_id="my_loyalty")
async def my_loyalty(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    acct = await _my_account(db, user)
    la = await loyalty._locked_loyalty(db, acct.id)  # get-or-create
    recent = await loyalty.recent_ledger(db, acct.id)
    await db.commit()
    return LoyaltyOut(
        account_id=acct.id,
        points_balance=la.points_balance,
        recent=[LedgerEntryOut.model_validate(r) for r in recent],
    )


# ---------- Customer: referral ----------
@router.get("/loyalty/referral-code", response_model=ReferralCodeOut, operation_id="my_referral_code")
async def my_referral_code(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    acct = await _my_account(db, user)
    rc = await loyalty.get_or_create_referral_code(db, acct.id)
    await db.commit()
    await db.refresh(rc)
    return rc


@router.post("/loyalty/referral/redeem", response_model=ReferralRedeemedOut, operation_id="redeem_referral")
async def redeem_referral(
    data: RedeemReferralIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    acct = await _my_account(db, user)
    rc = await loyalty.redeem_referral(db, data.code, acct.id)
    await db.commit()
    return ReferralRedeemedOut(detail="Referral redeemed", code=rc.code)


# ---------- Customer: gift cards ----------
@router.get("/loyalty/gift-cards", response_model=list[GiftCardOut], operation_id="my_gift_cards")
async def my_gift_cards(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    acct = await _my_account(db, user)
    await db.commit()
    rows = (
        await db.execute(
            select(GiftCard).where(GiftCard.issued_to_account_id == acct.id).order_by(GiftCard.id.desc()).limit(500)
        )
    ).scalars().all()
    return rows


@router.get(
    "/loyalty/gift-cards/{code}/balance",
    response_model=GiftCardOut,
    operation_id="gift_card_balance",
)
async def gift_card_balance(
    code: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Check a gift card's remaining balance/status by its code (before redeeming)."""
    gc = await db.scalar(select(GiftCard).where(GiftCard.code == code))
    # A card issued to a specific account is visible only to that account (404 — same
    # non-enumeration rule as redeem).
    if gc is None or (
        gc.issued_to_account_id is not None
        and user.role != "admin"
        and gc.issued_to_account_id != (await _my_account(db, user)).id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gift card not found")
    return gc


@router.post("/loyalty/gift-cards/redeem", response_model=GiftCardOut, operation_id="redeem_gift_card")
async def redeem_gift_card(
    data: RedeemGiftIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    acct = await _my_account(db, user)
    gc = await loyalty.redeem_gift_card(db, data.code, acct.id, data.amount)
    await db.commit()
    await db.refresh(gc)
    return gc


# ---------- Admin ----------
@router.post(
    "/admin/loyalty/{account_id}/adjust",
    response_model=LoyaltyOut,
    operation_id="admin_adjust_points",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_adjust_points(account_id: int, data: AdjustPointsIn, db: AsyncSession = Depends(get_db)):
    if await db.get(CustomerAccount, account_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    la = await loyalty.adjust_points(db, account_id, data.points, data.reason)
    await db.commit()
    recent = await loyalty.recent_ledger(db, account_id)
    return LoyaltyOut(
        account_id=account_id,
        points_balance=la.points_balance,
        recent=[LedgerEntryOut.model_validate(r) for r in recent],
    )


@router.post(
    "/admin/gift-cards",
    response_model=GiftCardOut,
    operation_id="admin_issue_gift_card",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_issue_gift_card(data: IssueGiftIn, db: AsyncSession = Depends(get_db)):
    if data.issued_to_account_id is not None and await db.get(CustomerAccount, data.issued_to_account_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    gc = await loyalty.issue_gift_card(db, data.amount, data.issued_to_account_id, data.expires_at)
    await db.commit()
    await db.refresh(gc)
    return gc


@router.get(
    "/admin/loyalty",
    response_model=list[LoyaltyAccountAdminOut],
    operation_id="admin_list_loyalty",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_list_loyalty(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    rows = (
        await db.execute(
            select(LoyaltyAccount).order_by(LoyaltyAccount.points_balance.desc())
            .limit(page.limit).offset(page.offset)
        )
    ).scalars().all()
    return rows


@router.get(
    "/admin/gift-cards",
    response_model=list[GiftCardAdminOut],
    operation_id="admin_list_gift_cards",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_list_gift_cards(
    status_filter: str | None = None,
    issued_to: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(GiftCard).order_by(GiftCard.id.desc())
    if status_filter:
        q = q.where(GiftCard.status == status_filter)
    if issued_to is not None:
        q = q.where(GiftCard.issued_to_account_id == issued_to)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@router.post(
    "/admin/gift-cards/{card_id}/void",
    response_model=GiftCardAdminOut,
    operation_id="admin_void_gift_card",
    dependencies=[Depends(require_role("admin"))],
)
async def admin_void_gift_card(card_id: int, db: AsyncSession = Depends(get_db)):
    gc = (
        await db.execute(select(GiftCard).where(GiftCard.id == card_id).with_for_update())
    ).scalar_one_or_none()
    if gc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gift card not found")
    if gc.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Gift card is {gc.status}, cannot void")
    gc.status = "disabled"
    await db.commit()
    await db.refresh(gc)
    return gc
