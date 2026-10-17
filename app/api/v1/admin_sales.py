"""Admin read/management grids the Refine dashboard needs: payments, refunds,
customer accounts, and order management (filters + cancel)."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.billing import Wallet
from app.models.order import Order
from app.models.payment import Payment
from app.models.refund import Refund
from app.models.shop import CustomerAccount
from app.models.subscription import Subscription
from app.models.user import User
from app.services import billing as billing_svc

router = APIRouter(
    prefix="/admin", tags=["admin-sales"], dependencies=[Depends(require_role("admin"))]
)


# ---------- schemas ----------
class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    payment_type: str
    gateway: str
    amount: float
    status: str
    ref_type: str | None = None
    ref_id: int | None = None
    gateway_order_id: str | None = None
    created_at: dt.datetime


class RefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    payment_id: int
    account_id: int
    amount: float
    status: str
    reason: str | None = None
    created_at: dt.datetime


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_user_id: int
    account_type: str
    billing_mode: str
    price_tier_id: int | None = None


class AccountRow(AccountOut):
    phone: str | None = None
    full_name: str | None = None


class AccountDetailOut(AccountRow):
    wallet_balance: float
    credit_outstanding: float
    order_count: int
    subscription_count: int


class OrderAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_no: str
    account_id: int
    shop_id: int
    status: str
    payment_status: str
    total: float


# ---------- payments ----------
@router.get("/payments", response_model=list[PaymentOut], operation_id="admin_list_payments")
async def list_payments(
    status_filter: str | None = None,
    account_id: int | None = None,
    ref_type: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Payment).order_by(Payment.id.desc())
    if status_filter:
        q = q.where(Payment.status == status_filter)
    if account_id is not None:
        q = q.where(Payment.account_id == account_id)
    if ref_type:
        q = q.where(Payment.ref_type == ref_type)
    if date_from:
        q = q.where(Payment.created_at >= date_from)
    if date_to:
        q = q.where(Payment.created_at < date_to + dt.timedelta(days=1))
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@router.get("/payments/{payment_id}", response_model=PaymentOut, operation_id="admin_get_payment")
async def get_payment(payment_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(Payment, payment_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    return row


# ---------- refunds ----------
@router.get("/refunds", response_model=list[RefundOut], operation_id="admin_list_refunds")
async def list_refunds(
    status_filter: str | None = None,
    payment_id: int | None = None,
    account_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Refund).order_by(Refund.id.desc())
    if status_filter:
        q = q.where(Refund.status == status_filter)
    if payment_id is not None:
        q = q.where(Refund.payment_id == payment_id)
    if account_id is not None:
        q = q.where(Refund.account_id == account_id)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@router.get("/refunds/{refund_id}", response_model=RefundOut, operation_id="admin_get_refund")
async def get_refund(refund_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(Refund, refund_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Refund not found")
    return row


# ---------- customer accounts ----------
@router.get("/accounts", response_model=list[AccountRow], operation_id="admin_list_accounts")
async def list_accounts(
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    query = select(CustomerAccount, User).join(User, User.id == CustomerAccount.owner_user_id)
    if q:
        like = f"%{q}%"
        query = query.where((User.phone.ilike(like)) | (User.full_name.ilike(like)))
    query = query.order_by(CustomerAccount.id.desc()).limit(page.limit).offset(page.offset)
    rows = (await db.execute(query)).all()
    out = []
    for acct, user in rows:
        r = AccountRow.model_validate(acct)
        r.phone = user.phone
        r.full_name = user.full_name
        out.append(r)
    return out


@router.get("/accounts/{account_id}", response_model=AccountDetailOut, operation_id="admin_get_account")
async def get_account(account_id: int, db: AsyncSession = Depends(get_db)):
    acct = await db.get(CustomerAccount, account_id)
    if acct is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    user = await db.get(User, acct.owner_user_id)
    wallet = await db.scalar(select(Wallet).where(Wallet.account_id == account_id))
    credit = await billing_svc.credit_outstanding(db, account_id)
    order_count = await db.scalar(
        select(func.count()).select_from(Order).where(Order.account_id == account_id)
    )
    sub_count = await db.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.account_id == account_id)
    )
    return AccountDetailOut(
        id=acct.id,
        owner_user_id=acct.owner_user_id,
        account_type=acct.account_type,
        billing_mode=acct.billing_mode,
        price_tier_id=acct.price_tier_id,
        phone=user.phone if user else None,
        full_name=user.full_name if user else None,
        wallet_balance=float(wallet.balance) if wallet else 0.0,
        credit_outstanding=float(credit),
        order_count=int(order_count or 0),
        subscription_count=int(sub_count or 0),
    )


# ---------- order management ----------
@router.get("/orders", response_model=list[OrderAdminOut], operation_id="admin_list_orders")
async def list_orders(
    status_filter: str | None = None,
    payment_status: str | None = None,
    account_id: int | None = None,
    shop_id: int | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Order).order_by(Order.id.desc())
    if status_filter:
        q = q.where(Order.status == status_filter)
    if payment_status:
        q = q.where(Order.payment_status == payment_status)
    if account_id is not None:
        q = q.where(Order.account_id == account_id)
    if shop_id is not None:
        q = q.where(Order.shop_id == shop_id)
    if date_from:
        q = q.where(Order.created_at >= date_from)
    if date_to:
        q = q.where(Order.created_at < date_to + dt.timedelta(days=1))
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@router.post("/orders/{order_id}/cancel", response_model=OrderAdminOut, operation_id="admin_cancel_order")
async def cancel_order(order_id: int, db: AsyncSession = Depends(get_db)):
    order = (
        await db.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status == "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "Order already cancelled")
    if order.payment_status in ("refunded", "partially_refunded"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Refunded order cannot be cancelled")
    if order.payment_status == "paid":
        # Never cancel a paid order silently (money would be kept). Refund it first via
        # POST /payments/{id}/refund — that reverses the charge and issues a credit note.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Order is paid; refund the payment before cancelling",
        )
    order.status = "cancelled"
    await db.commit()
    await db.refresh(order)
    return order
