import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.payment import Payment
from app.models.refund import Refund, WebhookEvent
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.payment import PaymentCreate, PaymentCreateOut, PaymentVerify, RefundIn
from app.services import billing as billing_service
from app.services import invoicing
from app.services import notifications as notify
from app.services import payments as pay_service


async def _owner_user(db: AsyncSession, account_id: int) -> User | None:
    account = await db.get(CustomerAccount, account_id)
    return await db.get(User, account.owner_user_id) if account else None


router = APIRouter(prefix="/payments", tags=["payments"])


class PaymentVerifyOut(BaseModel):
    detail: str
    payment_id: int
    status: str


class RefundOut(BaseModel):
    refund_id: int
    payment_id: int
    amount: float
    status: str


class WebhookAck(BaseModel):
    status: str


async def _finalize_payment(db: AsyncSession, payment: Payment, gateway_payment_id: str) -> None:
    """Mark a payment successful + apply its effect (wallet credit / order paid).
    Caller must hold a row lock on `payment` and guarantee status == 'initiated'."""
    payment.status = "success"
    payment.gateway_payment_id = gateway_payment_id
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


async def _account(db: AsyncSession, user: User) -> CustomerAccount:
    res = await db.execute(select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id))
    account = res.scalar_one_or_none()
    if account is None:
        # Race-safe create: a concurrent first-touch may win uq_customer_accounts_owner;
        # on collision re-select the row the other transaction committed.
        try:
            async with db.begin_nested():
                account = CustomerAccount(
                    owner_user_id=user.id, account_type="retail", billing_mode="prepaid_wallet"
                )
                db.add(account)
                await db.flush()
        except IntegrityError:
            account = (
                await db.execute(
                    select(CustomerAccount).where(CustomerAccount.owner_user_id == user.id)
                )
            ).scalar_one()
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
        # Lock the order so two concurrent creates can't both open a gateway payment
        # (which would double-charge the customer).
        res = await db.execute(
            select(Order).where(Order.id == data.order_id).with_for_update()
        )
        order = res.scalar_one_or_none()
        if order is None or order.account_id != account.id:  # ownership
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
        if order.payment_status == "paid":
            raise HTTPException(status.HTTP_409_CONFLICT, "Order already paid")
        # One open/settled payment per order at a time.
        open_pay = await db.scalar(
            select(func.count())
            .select_from(Payment)
            .where(
                Payment.ref_type == "order",
                Payment.ref_id == order.id,
                Payment.status.in_(("initiated", "success")),
            )
        )
        if open_pay:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A payment for this order is already in progress"
            )
        amount = float(order.total)  # server-authoritative, ignore client amount
        ref_type = "order"
        ref_id = order.id
    else:  # wallet_topup — client amount is authoritative and required
        if amount is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "amount required for top-up")

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


@router.post("/verify", operation_id="verify_payment", response_model=PaymentVerifyOut)
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

    await _finalize_payment(db, payment, data.gateway_payment_id)
    await db.commit()
    return {"detail": "payment successful", "payment_id": payment.id, "status": payment.status}


@router.post(
    "/{payment_id}/refund",
    operation_id="refund_payment",
    response_model=RefundOut,
    dependencies=[Depends(require_role("admin"))],
)
async def refund_payment(
    payment_id: int, data: RefundIn, db: AsyncSession = Depends(get_db)
):
    """Refund a successful payment (full or partial). Idempotency-Key recommended.
    Top-up refunds reverse the wallet credit; order-payment refunds return store
    credit to the wallet and mark the order refunded. The payment row is locked
    FOR UPDATE, so concurrent refunds for the same payment serialize."""
    res = await db.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    payment = res.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    if payment.status != "success":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only successful payments can be refunded")

    already = float(
        await db.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0)).where(
                Refund.payment_id == payment.id, Refund.status == "processed"
            )
        )
        or 0
    )
    remaining = round(float(payment.amount) - already, 2)
    amount = round(float(data.amount), 2) if data.amount is not None else remaining
    if amount <= 0 or amount > remaining:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Refund amount must be in (0, {remaining}]",
        )

    refund = Refund(
        payment_id=payment.id,
        account_id=payment.account_id,
        amount=amount,
        reason=data.reason,
        status="initiated",
    )
    db.add(refund)
    await db.flush()
    # The gateway call can time out. If it fails, persist the refund as 'failed'
    # (audit trail, no wallet effect — it never counts toward `already`) and surface
    # 502 so an admin can retry with a fresh refund, rather than a silent stuck row.
    try:
        refund.gateway_refund_id = await pay_service.create_gateway_refund(
            payment.gateway_payment_id, amount
        )
    except Exception as exc:
        refund.status = "failed"
        refund.reason = (refund.reason or "") + f" [gateway error: {str(exc)[:80]}]"
        await db.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Refund could not be processed by the gateway; no money moved. Please retry.",
        ) from exc
    # The gateway has moved the money. Persist the refund as 'processed' and COMMIT
    # NOW — before touching the wallet/order — so a retry after any later failure sees
    # this refund in `already` and can never issue a second gateway refund.
    refund.status = "processed"
    await db.commit()

    # Apply the ledger effect in a follow-up transaction. If it fails, the refund is
    # already recorded (no double-refund); surface 502 for manual reconciliation rather
    # than rolling back the record of money that already left the gateway.
    try:
        if payment.payment_type == "wallet_topup":
            # Reverse the top-up: pull the money back out of the wallet (the gateway
            # refund above already returned it to the customer's card/UPI).
            wallet = await billing_service.wallet_adjust(
                db, payment.account_id, -amount, "refund_reversal", "refund", refund.id,
                allow_negative=True,
            )
            balance = float(wallet.balance)
        else:
            # order_payment -> refunded TO SOURCE (card/UPI) by the gateway refund above.
            # Do NOT also credit the wallet (that was a double-refund). Mark the order and,
            # once it's fully refunded, issue the GST credit note against its invoice.
            wallet = await billing_service.get_or_create_wallet(db, payment.account_id)
            balance = float(wallet.balance)
            if payment.ref_id:
                order = await db.get(Order, payment.ref_id)
                if order is not None:
                    fully = round(already + amount, 2) >= round(float(payment.amount), 2)
                    order.payment_status = "refunded" if fully else "partially_refunded"
                    if fully:
                        inv = await db.scalar(
                            select(Invoice).where(
                                Invoice.order_id == order.id,
                                Invoice.is_credit_note.is_(False),
                            )
                        )
                        if inv is not None:
                            await invoicing.generate_credit_note(
                                db, inv, reason=f"Refund for order {order.order_no}"
                            )
        # Tell the customer their refund landed (idempotent per refund; emit is isolated).
        owner = await _owner_user(db, payment.account_id)
        if owner is not None:
            await notify.emit_event(
                db, "payment.refunded", owner,
                {"amount": amount, "balance": balance},
                account_id=payment.account_id, dedupe_key=f"payment.refunded:{refund.id}",
            )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Refund {refund.id} recorded at gateway but the wallet/order update failed; "
            "it will not be re-refunded — reconcile the ledger manually.",
        ) from exc
    return {
        "refund_id": refund.id,
        "payment_id": payment.id,
        "amount": amount,
        "status": refund.status,
    }


@router.post("/webhook", operation_id="razorpay_webhook", response_model=WebhookAck)
async def razorpay_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Gateway callback. Signature-verified, idempotent by event id. Unknown events
    are acknowledged. Finalizes a captured payment that the client never verified."""
    raw = await request.body()
    sig = request.headers.get("X-Razorpay-Signature")
    if not pay_service.verify_webhook_signature(raw, sig):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid webhook signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON") from None

    event_type = payload.get("event")
    event_id = (
        request.headers.get("X-Razorpay-Event-Id")
        or payload.get("id")
        or hashlib.sha256(raw or b"").hexdigest()
    )
    # Record the dedupe row AND process the event in ONE transaction, committing once
    # at the end — so the dedupe marker only becomes durable if the side effect also
    # committed. (Previously the marker was committed first; a handler failure then let
    # the retry be dismissed as "duplicate", permanently dropping payment finalization.)
    try:
        async with db.begin_nested():  # savepoint: detect duplicate event_id
            db.add(WebhookEvent(event_id=event_id, event_type=event_type, payload=payload))
            await db.flush()
    except IntegrityError:
        return {"status": "duplicate"}

    await _handle_webhook_event(db, event_type, payload)
    await db.commit()
    return {"status": "ok"}


def _payment_ids_from_payload(payload: dict) -> tuple[str | None, str | None]:
    """Pull (order_id, payment_id) from the Razorpay nested shape or a flat test shape."""
    try:
        entity = payload["payload"]["payment"]["entity"]
        return entity.get("order_id"), entity.get("id")
    except (KeyError, TypeError):
        return payload.get("order_id"), payload.get("payment_id")


async def _handle_webhook_event(db: AsyncSession, event_type: str | None, payload: dict) -> None:
    if event_type not in ("payment.captured", "payment.authorized", "payment.failed"):
        return  # other events are logged only (MVP)
    order_id, gpid = _payment_ids_from_payload(payload)
    if not order_id:
        return
    res = await db.execute(
        select(Payment).where(Payment.gateway_order_id == order_id).with_for_update()
    )
    payment = res.scalar_one_or_none()
    if payment is None or payment.status != "initiated":
        return
    # No commits here — the caller (razorpay_webhook) commits the event record + this
    # side effect together in one transaction.
    if event_type == "payment.failed":
        # Server-side failure the client never surfaced: mark failed + notify so the
        # order isn't left stuck 'initiated'. The key is released for a fresh attempt.
        payment.status = "failed"
        owner = await _owner_user(db, payment.account_id)
        if owner is not None:
            await notify.emit_event(
                db, "payment.failed", owner, {"amount": float(payment.amount)},
                account_id=payment.account_id, dedupe_key=f"payment.failed:{payment.id}",
            )
        return
    await _finalize_payment(db, payment, gpid or "webhook")
