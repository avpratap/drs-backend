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
from app.services import payments as pay_service

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


@router.post("/create", response_model=PaymentCreateOut, operation_id="create_payment")
async def create_payment(
    data: PaymentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if data.purpose not in ("wallet_topup", "order_payment"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid purpose")
    account = await _account(db, user)
    gateway_order_id = await pay_service.create_gateway_order(data.amount)
    payment = Payment(
        account_id=account.id,
        payment_type=data.purpose,
        gateway="razorpay",
        gateway_order_id=gateway_order_id,
        amount=data.amount,
        status="initiated",
        ref_type="order" if data.purpose == "order_payment" else "topup",
        ref_id=data.order_id,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return PaymentCreateOut(
        payment_id=payment.id,
        gateway_order_id=gateway_order_id,
        amount=data.amount,
        key_id=settings.RAZORPAY_KEY_ID,
    )


@router.post("/verify", operation_id="verify_payment")
async def verify_payment(
    data: PaymentVerify,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(Payment).where(Payment.gateway_order_id == data.gateway_order_id)
    )
    payment = res.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")

    if not pay_service.verify_signature(
        data.gateway_order_id, data.gateway_payment_id, data.signature
    ):
        payment.status = "failed"
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Signature verification failed")

    if payment.status != "success":  # idempotent finalize
        payment.status = "success"
        payment.gateway_payment_id = data.gateway_payment_id
        if payment.payment_type == "wallet_topup":
            await billing_service.wallet_adjust(
                db, payment.account_id, float(payment.amount), "topup", "payment", payment.id
            )
        elif payment.payment_type == "order_payment" and payment.ref_id:
            order = await db.get(Order, payment.ref_id)
            if order is not None:
                order.payment_status = "paid"
                order.status = "confirmed"
        await db.commit()

    return {"detail": "payment successful", "payment_id": payment.id, "status": payment.status}
