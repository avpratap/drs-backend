from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps.auth import get_current_user
from app.models.order import Order
from app.models.payment import Payment
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.payment import PaymentCreate, PaymentCreateOut, PaymentVerify
from app.services import billing as billing_service
from app.services import notifications as notify
from app.services import payments as pay_service


async def _owner_user(db: AsyncSession, account_id: int) -> User | None:
    account = await db.get(CustomerAccount, account_id)
    return await db.get(User, account.owner_user_id) if account else None

router = APIRouter(prefix="/payments", tags=["payments"])


async def _account(db: AsyncSession, user: User) -> CustomerAccount:
    res = await db.execute(select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id))
    account = res.scalar_one_or_none()
    if account is None:
        account = CustomerAccount(
            owner_user_id=user.id, account_type="retail", billing_mode="prepaid_wallet"
        )
        db.add(account)
        await db.flush()
    return account


async def _my_account_ids(db: AsyncSession, user: User) -> set[int]:
    res = await db.execute(
        select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id)
    )
    return set(res.scalars().all())


@router.post("/create", response_model=PaymentCreateOut, operation_id="create_payment")
async def create_payment(
    data: PaymentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if data.purpose not in ("wallet_topup", "order_payment"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid purpose")
    account = await _account(db, user)

    amount = data.amount
    ref_type = "topup"
    ref_id = None
    if data.purpose == "order_payment":
        if not data.order_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "order_id required")
        order = await db.get(Order, data.order_id)
        if order is None or order.account_id != account.id:  # ownership
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
        if order.payment_status == "paid":
            raise HTTPException(status.HTTP_409_CONFLICT, "Order already paid")
        amount = float(order.total)  # server-authoritative, ignore client amount
        ref_type = "order"
        ref_id = order.id

    gateway_order_id = await pay_service.create_gateway_order(amount)
    payment = Payment(
        account_id=account.id,
        payment_type=data.purpose,
        gateway="razorpay",
        gateway_order_id=gateway_order_id,
        amount=amount,
        status="initiated",
        ref_type=ref_type,
        ref_id=ref_id,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return PaymentCreateOut(
        payment_id=payment.id,
        gateway_order_id=gateway_order_id,
        amount=amount,
        key_id=settings.RAZORPAY_KEY_ID,
    )


@router.post("/verify", operation_id="verify_payment")
async def verify_payment(
    data: PaymentVerify,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Payment).where(Payment.gateway_order_id == data.gateway_order_id).with_for_update()
    )
    payment = res.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")

    # Ownership: a user may only finalize their own payment (admin may finalize any).
    if user.role != "admin" and payment.account_id not in await _my_account_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")

    if payment.status == "success":  # idempotent replay
        return {"detail": "payment successful", "payment_id": payment.id, "status": "success"}
    if payment.status != "initiated":  # e.g. 'failed' — don't re-finalize; start a new payment
        raise HTTPException(status.HTTP_409_CONFLICT, f"Payment is {payment.status}")

    if not pay_service.verify_signature(
        data.gateway_order_id, data.gateway_payment_id, data.signature
    ):
        payment.status = "failed"
        owner = await _owner_user(db, payment.account_id)
        if owner is not None:
            await notify.emit_event(
                db,
                "payment.failed",
                owner,
                {"amount": float(payment.amount)},
                account_id=payment.account_id,
                dedupe_key=f"payment.failed:{payment.id}",
            )
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Signature verification failed")

    payment.status = "success"
    payment.gateway_payment_id = data.gateway_payment_id
    if payment.payment_type == "wallet_topup":
        wallet = await billing_service.wallet_adjust(
            db, payment.account_id, float(payment.amount), "topup", "payment", payment.id
        )
        owner = await _owner_user(db, payment.account_id)
        if owner is not None:
            await notify.emit_event(
                db,
                "payment.success",
                owner,
                {"amount": float(payment.amount), "balance": float(wallet.balance)},
                account_id=payment.account_id,
                dedupe_key=f"payment.success:{payment.id}",
            )
    elif payment.payment_type == "order_payment" and payment.ref_id:
        order = await db.get(Order, payment.ref_id)
        if order is not None and order.account_id == payment.account_id:
            order.payment_status = "paid"
            order.status = "confirmed"
    await db.commit()
    return {"detail": "payment successful", "payment_id": payment.id, "status": payment.status}
