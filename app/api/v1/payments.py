import datetime as dt
import hashlib
import json
import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.idempotency import require_idempotency_key
from app.models.billing import WalletTransaction
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.payment import Payment
from app.models.refund import Refund, WebhookEvent
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.payment import PaymentCreate, PaymentCreateOut, PaymentVerify, RefundIn
from app.services import admin_tools as audit_svc
from app.services import billing as billing_service
from app.services import invoicing
from app.services import notifications as notify
from app.services import payments as pay_service
from app.services import profile as profile_svc
from app.services import refunds as refund_acct

log = logging.getLogger("drs.payments")


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
    Caller must hold a row lock on `payment` and guarantee status != 'success'."""
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
        order = await refund_acct.lock_order(db, payment.ref_id)
        if order is None or order.account_id != payment.account_id:
            payment.is_excess = True
            return
        if order.payment_status != "pending":
            # Another payment already settled this order: this capture is a duplicate.
            # It is refunded on its own (is_excess) and never reverses the order/invoice.
            log.warning(
                "payment %s is a duplicate capture for order %s (%s) — needs refund",
                payment.id, order.id, order.payment_status,
            )
            payment.is_excess = True
            return
        if order.status == "cancelled":
            # Money was captured for an order that was cancelled meanwhile. Record the
            # capture — it must be refunded — but never resurrect a cancelled order to
            # 'confirmed' (its coupon was released).
            log.warning(
                "payment %s captured for cancelled order %s — needs refund", payment.id, order.id
            )
            order.payment_status = "paid"
            return
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
                    owner_user_id=user.id, account_type=profile_svc.account_type_for(user), billing_mode="prepaid_wallet"
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


# A 'creating' payment older than this belongs to a request that died before the
# gateway answered (the HTTP timeout is 10s): it no longer blocks a new checkout.
CREATING_STALE_AFTER = dt.timedelta(minutes=1)


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
        if order.status not in ("created", "pending") or order.payment_status != "pending":
            # A cancelled (or refunded) order must not be payable — paying it would
            # resurrect it after its coupon reservation was released.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Order is {order.status}/{order.payment_status} and cannot be paid",
            )
        # One open payment per order: RESUME an unfinished checkout (the customer closed
        # the gateway modal) instead of refusing forever; never open a second one.
        open_pay = (
            await db.execute(
                select(Payment)
                .where(
                    Payment.ref_type == "order",
                    Payment.ref_id == order.id,
                    Payment.status.in_(("creating", "initiated", "success")),
                )
                .order_by(Payment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if open_pay is not None and open_pay.status == "creating":
            if open_pay.created_at > dt.datetime.now(dt.UTC) - CREATING_STALE_AFTER:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "A checkout for this order is being opened; retry"
                )
            open_pay.status = "failed"  # its request died before the gateway answered
            open_pay = None
        if open_pay is not None:
            if open_pay.status == "success":
                raise HTTPException(status.HTTP_409_CONFLICT, "Order already paid")
            await db.commit()
            return PaymentCreateOut(
                payment_id=open_pay.id,
                gateway_order_id=open_pay.gateway_order_id,
                amount=float(open_pay.amount),
                key_id=settings.RAZORPAY_KEY_ID,
            )
        amount = round(float(order.total), 2)  # server-authoritative, ignore client amount
        ref_type = "order"
        ref_id = order.id
        if amount <= 0:
            # Fully discounted (coupon) order: nothing to collect, and the gateway rejects
            # sub-₹1 orders. Settle it without the gateway.
            payment = Payment(
                account_id=account.id, payment_type=data.purpose, gateway="none",
                gateway_order_id=f"free_{secrets.token_hex(8)}", amount=0,
                status="initiated", ref_type=ref_type, ref_id=ref_id,
            )
            db.add(payment)
            await db.flush()
            await _finalize_payment(db, payment, "free")
            await db.commit()
            return PaymentCreateOut(
                payment_id=payment.id, gateway_order_id=payment.gateway_order_id,
                amount=0.0, key_id=settings.RAZORPAY_KEY_ID,
            )
    else:  # wallet_topup — client amount is authoritative and required
        if amount is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "amount required for top-up")
        # Quantize to 2dp ONCE so the gateway charge (round(amount*100) paise) and the
        # stored/credited Numeric(12,2) can't diverge at sub-paisa inputs (free balance).
        amount = round(float(amount), 2)
        if amount <= 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "amount must be > 0")

    payment = Payment(
        account_id=account.id,
        payment_type=data.purpose,
        gateway="razorpay",
        amount=amount,
        status="creating",
        ref_type=ref_type,
        ref_id=ref_id,
    )
    db.add(payment)
    # Commit the 'creating' placeholder BEFORE the gateway HTTP call: it is the order's one
    # open payment (a concurrent create sees it and backs off), and the Order row lock is
    # released instead of being held across a call that can take the full timeout.
    await db.commit()
    try:
        gateway_order_id = await pay_service.create_gateway_order(amount)
    except httpx.HTTPError as exc:
        log.warning("gateway order create failed: %s", exc)
        payment.status = "failed"
        await db.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Payment gateway unavailable; try again"
        ) from None
    payment.gateway_order_id = gateway_order_id
    payment.status = "initiated"
    await db.commit()
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

    if not pay_service.verify_signature(
        data.gateway_order_id, data.gateway_payment_id, data.signature
    ):
        # A bad/garbled signature proves nothing about the payment: do NOT mark it failed
        # (that permanently blocked a later genuine capture). Leave it open.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Signature verification failed")

    # A valid signature means the gateway captured the money — finalize even if a prior
    # attempt on this gateway order was reported failed.
    await _finalize_payment(db, payment, data.gateway_payment_id)
    await db.commit()
    return {"detail": "payment successful", "payment_id": payment.id, "status": payment.status}


async def _topup_hold(db: AsyncSession, refund_id: int) -> bool:
    """Whether the wallet hold for a top-up refund was taken (and not released)."""
    moved = await db.scalar(
        select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
            WalletTransaction.ref_type == "refund", WalletTransaction.ref_id == refund_id
        )
    )
    return float(moved or 0) < 0


async def _apply_refund_effect(db: AsyncSession, payment: Payment, refund: Refund) -> float:
    """Apply a PROCESSED refund's ledger effect exactly once (marker: ledger_applied_at),
    so admin reconciliation can safely re-run it after a failure. Returns wallet balance.
    No commit — the caller commits the effect together with the marker."""
    if refund.ledger_applied_at is not None:
        return float((await billing_service.get_or_create_wallet(db, payment.account_id)).balance)
    amount = float(refund.amount)
    if payment.payment_type == "wallet_topup":
        # Reverse the top-up: the money is pulled back out of the wallet when the refund
        # is INITIATED (a hold, so it can't be spent/withdrawn during the gateway call);
        # only a refund recorded before that hold existed is debited here.
        if await _topup_hold(db, refund.id):
            wallet = await billing_service.get_or_create_wallet(db, payment.account_id)
        else:
            wallet = await billing_service.wallet_adjust(
                db, payment.account_id, -amount, "refund_reversal", "refund", refund.id,
                allow_negative=True,
            )
    else:
        # order_payment -> refunded TO SOURCE (card/UPI). Do NOT also credit the wallet.
        # A duplicate (excess) capture is handed back without touching the order it never
        # settled: its status and tax invoice belong to the payment that did.
        wallet = await billing_service.get_or_create_wallet(db, payment.account_id)
        order = (
            await refund_acct.lock_order(db, payment.ref_id)
            if payment.ref_id and not payment.is_excess else None
        )
        if order is not None:
            processed = float(
                await db.scalar(
                    select(func.coalesce(func.sum(Refund.amount), 0)).where(
                        Refund.payment_id == payment.id, Refund.status == "processed"
                    )
                )
                or 0
            )
            # Fully refunded once nothing is left across BOTH paths (RMA store credit +
            # refunds to source), e.g. ₹500 RMA + ₹1000 to source on a ₹1500 order.
            fully = (
                round(processed, 2) >= round(float(payment.amount), 2)
                or await refund_acct.order_refund_headroom(db, order) <= 0
            )
            order.payment_status = "refunded" if fully else "partially_refunded"
            inv = await db.scalar(
                select(Invoice).where(
                    Invoice.order_id == order.id, Invoice.is_credit_note.is_(False)
                )
            )
            if inv is not None:
                if fully:
                    await invoicing.build_full_credit_note(
                        db, inv, reason=f"Refund for order {order.order_no}"
                    )
                else:
                    # Partial refund to source (no goods back): value-only credit note so
                    # output GST isn't over-reported.
                    await invoicing.generate_credit_note_for_lines(
                        db, inv, None, reason=f"Partial refund for order {order.order_no}",
                        amount=amount,
                    )
    refund.ledger_applied_at = dt.datetime.now(dt.UTC)
    owner = await _owner_user(db, payment.account_id)
    if owner is not None:
        await notify.emit_event(
            db, "payment.refunded", owner,
            {"amount": amount, "balance": float(wallet.balance)},
            account_id=payment.account_id, dedupe_key=f"payment.refunded:{refund.id}",
        )
    return float(wallet.balance)


@router.post(
    "/{payment_id}/refund",
    operation_id="refund_payment",
    response_model=RefundOut,
    dependencies=[Depends(require_role("admin")), Depends(require_idempotency_key)],
)
async def refund_payment(
    payment_id: int,
    data: RefundIn,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Refund a successful payment (full or partial). Top-up refunds reverse the wallet
    credit; order-payment refunds return money to source and mark the order refunded.

    Double-refund safety: the payment row is locked FOR UPDATE for the whole read-cap +
    insert-'initiated' section; the cap counts EVERY non-'failed' refund; and while any
    refund on the payment is unresolved (initiated/pending — the gateway outcome is
    unknown) a NEW refund is refused, so retrying a timed-out partial refund can never
    issue a second real one. Unresolved refunds are settled via the reconcile endpoint."""
    res = await db.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
    payment = res.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    if payment.status != "success":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only successful payments can be refunded")
    if payment.gateway == "none":
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing was charged for this payment")

    unresolved = await db.scalar(
        select(func.count()).select_from(Refund).where(
            Refund.payment_id == payment.id, Refund.status.in_(("initiated", "pending"))
        )
    )
    if unresolved:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A refund on this payment has an unknown gateway outcome; reconcile it first",
        )
    # Count every refund that is NOT definitively failed toward the cap.
    already = float(
        await db.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0)).where(
                Refund.payment_id == payment.id, Refund.status != "failed"
            )
        )
        or 0
    )
    remaining = round(float(payment.amount) - already, 2)
    if payment.payment_type == "order_payment" and payment.ref_id and not payment.is_excess:
        # One cap across RMA store-credit refunds AND refunds to source (under the order
        # lock), so an order can't be refunded twice through the two paths.
        order = await refund_acct.lock_order(db, payment.ref_id)
        if order is not None:
            remaining = min(remaining, await refund_acct.order_refund_headroom(db, order))
    elif payment.payment_type == "wallet_topup":
        # A top-up that was already spent/withdrawn can't be refunded to the card too.
        wallet = await billing_service.get_or_create_wallet(db, payment.account_id, lock=True)
        remaining = min(remaining, max(round(float(wallet.balance), 2), 0.0))
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
    if payment.payment_type == "wallet_topup":
        # Hold the money now, under the wallet lock the cap was computed with: committing
        # 'initiated' releases that lock, and a withdrawal/delivery debit during the
        # gateway call could otherwise spend the same balance a second time.
        await billing_service.wallet_adjust(
            db, payment.account_id, -amount, "refund_reversal", "refund", refund.id,
            allow_negative=True,
        )
    await audit_svc.record_audit(
        db, actor.id, "payment_refund", "payment", payment.id,
        {"refund_id": refund.id, "amount": amount},
    )
    # COMMIT the 'initiated' row BEFORE the gateway call so refund.id is durable: a retry
    # after a crash reuses the same id → the same gateway Idempotency-Key → Razorpay
    # returns the original refund instead of issuing a second one.
    await db.commit()
    # The gateway call can time out AFTER Razorpay actually processed the refund, so a
    # failure here is AMBIGUOUS. Persist it as 'pending' (never 'failed'): it keeps
    # counting toward the cap and blocks new refunds until reconciled.
    refund_id = refund.id
    gateway_refund_id: str | None = None
    gateway_exc: Exception | None = None
    try:
        gateway_refund_id = await pay_service.create_gateway_refund(
            payment.gateway_payment_id, amount, idempotency_key=f"drs-refund-{refund_id}"
        )
    except Exception as exc:
        gateway_exc = exc
    # The locks were released by the commit above: re-lock Payment -> Refund (same order as
    # reconcile) and re-read the refund, so a reconcile that settled it meanwhile is seen
    # and its terminal status / applied effect are never overwritten or applied twice.
    payment, refund = await _lock_payment_refund(db, payment.id, refund_id)
    if refund.status != "initiated":
        log.warning(
            "refund %s was settled to %s during its gateway call (gateway %s)",
            refund_id, refund.status, "error" if gateway_exc else "ok",
        )
        if gateway_exc is None and refund.status == "failed":
            log.error("refund %s: gateway refunded but it was reconciled 'failed'", refund_id)
        if gateway_refund_id and not refund.gateway_refund_id:
            refund.gateway_refund_id = gateway_refund_id
        await db.commit()
        if gateway_exc is not None or refund.status != "processed":
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Refund {refund_id} was reconciled as {refund.status}"
            )
    elif gateway_exc is not None:
        refund.status = "pending"
        refund.reason = (
            (refund.reason or "") + f" [gateway error, needs reconcile: {str(gateway_exc)[:60]}]"
        )[:255]
        await db.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Refund is pending at the gateway and needs reconciliation; it was NOT retried "
            "automatically to avoid a double refund.",
        ) from gateway_exc
    else:
        # The gateway has moved the money: record 'processed' and COMMIT before the ledger
        # effect, so nothing later can cause a second gateway refund.
        refund.gateway_refund_id = gateway_refund_id
        refund.status = "processed"
        await db.commit()
        try:
            payment, refund = await _lock_payment_refund(db, payment.id, refund_id)
            await _apply_refund_effect(db, payment, refund)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            log.exception("refund %s ledger effect failed", refund_id)
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Refund {refund_id} recorded at gateway but the wallet/order update failed; "
                "it will not be re-refunded — re-apply it via the reconcile endpoint.",
            ) from exc
    return {
        "refund_id": refund.id,
        "payment_id": payment.id,
        "amount": amount,
        "status": refund.status,
    }


async def _lock_payment_refund(
    db: AsyncSession, payment_id: int, refund_id: int
) -> tuple[Payment, Refund]:
    """Lock Payment then Refund FOR UPDATE (the order every refund path uses), re-reading
    both so a change committed by a concurrent transaction is not missed."""
    payment = (
        await db.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    refund = (
        await db.execute(
            select(Refund).where(Refund.id == refund_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return payment, refund


# An 'initiated' refund younger than this may still have its gateway call in flight
# (httpx timeout 10s + margin): reconciling it 'failed' would free the cap while the
# gateway may still refund, allowing a second real refund.
RECONCILE_INITIATED_AFTER = dt.timedelta(minutes=10)


class RefundReconcileIn(BaseModel):
    # processed: the gateway did refund (apply the ledger effect); failed: it did not
    # (frees the cap). Re-posting 'processed' on a processed refund re-applies a missing
    # ledger effect (repair after a post-gateway failure) and is otherwise a no-op.
    outcome: str = Field(pattern="^(processed|failed)$")
    gateway_refund_id: str | None = Field(default=None, max_length=128)


@router.post(
    "/refunds/{refund_id}/reconcile",
    operation_id="reconcile_refund",
    response_model=RefundOut,
    dependencies=[Depends(require_role("admin"))],
)
async def reconcile_refund(
    refund_id: int,
    data: RefundReconcileIn,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin settles a refund whose gateway outcome was unknown (pending/initiated), or
    re-applies the ledger effect of a processed refund whose follow-up update failed."""
    refund = await db.get(Refund, refund_id)
    if refund is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Refund not found")
    payment, refund = await _lock_payment_refund(db, refund.payment_id, refund_id)
    if refund.status in ("failed",) or (refund.status == "processed" and data.outcome == "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Refund is already {refund.status}")
    if refund.status == "initiated" and refund.created_at > (
        dt.datetime.now(dt.UTC) - RECONCILE_INITIATED_AFTER
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Refund's gateway call may still be in flight; reconcile it later",
        )
    if data.outcome == "failed":
        refund.status = "failed"
        if payment.payment_type == "wallet_topup" and await _topup_hold(db, refund.id):
            # The gateway never refunded: give the held money back to the wallet.
            await billing_service.wallet_adjust(
                db, payment.account_id, float(refund.amount), "refund_release", "refund",
                refund.id,
            )
    else:
        refund.status = "processed"
        if data.gateway_refund_id:
            refund.gateway_refund_id = data.gateway_refund_id
        await _apply_refund_effect(db, payment, refund)
    await audit_svc.record_audit(
        db, actor.id, "refund_reconcile", "refund", refund.id, {"outcome": data.outcome}
    )
    await db.commit()
    return {
        "refund_id": refund.id,
        "payment_id": payment.id,
        "amount": float(refund.amount),
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
    # Only 'captured' (funds settled) finalizes. 'authorized' means funds are merely held
    # (auto-capture via payment_capture:1 follows as 'captured'); crediting on authorize
    # would credit money that could still fail to capture / be voided.
    if event_type not in ("payment.captured", "payment.failed"):
        return  # authorized + everything else are logged only
    order_id, gpid = _payment_ids_from_payload(payload)
    if not order_id:
        return
    res = await db.execute(
        select(Payment).where(Payment.gateway_order_id == order_id).with_for_update()
    )
    payment = res.scalar_one_or_none()
    if payment is None or payment.status == "success":
        return
    # No commits here — the caller (razorpay_webhook) commits the event record + this
    # side effect together in one transaction.
    if event_type == "payment.failed":
        # A failed ATTEMPT: Razorpay lets the customer retry on the same gateway order,
        # so this must not terminally fail the payment (a later 'captured' must still
        # finalize). Notify once and leave it open; checkout can be resumed.
        owner = await _owner_user(db, payment.account_id)
        if owner is not None:
            await notify.emit_event(
                db, "payment.failed", owner, {"amount": float(payment.amount)},
                account_id=payment.account_id, dedupe_key=f"payment.failed:{payment.id}",
            )
        return
    # 'captured' finalizes from initiated OR a legacy 'failed' (money was captured).
    await _finalize_payment(db, payment, gpid or "webhook")
