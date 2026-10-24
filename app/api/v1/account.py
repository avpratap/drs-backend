import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import media
from app.core.clientip import client_ip
from app.core.config import settings
from app.core.db import get_db
from app.core.sendlimit import allow_send
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.models.profile import Address, PaymentMandate, PaymentMethod
from app.models.user import User
from app.schemas.common import DataExportOut, EraseResultOut
from app.schemas.profile import (
    AddressIn,
    AddressOut,
    AddressUpdate,
    ConsentIn,
    ConsentOut,
    MandateIn,
    MandateOut,
    PaymentMethodIn,
    PaymentMethodOut,
    ProfileOut,
    ProfileUpdate,
)
from app.services import dpdp, email_verify
from app.services import profile as svc
from app.services.notifications import senders

router = APIRouter(tags=["account"])


def _client(request: Request) -> dict:
    return {
        "ip": client_ip(request),
        "ua": request.headers.get("user-agent", "")[:512] or None,
    }


async def _owned_address(db: AsyncSession, user: User, address_id: int) -> Address:
    addr = await db.get(Address, address_id)
    ids = await svc.my_account_ids(db, user)
    if addr is None or addr.account_id not in ids or addr.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Address not found")
    return addr


async def _owned_method(db: AsyncSession, user: User, pm_id: int) -> PaymentMethod:
    pm = await db.get(PaymentMethod, pm_id)
    ids = await svc.my_account_ids(db, user)
    if pm is None or pm.account_id not in ids or pm.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment method not found")
    return pm


# ---------- Profile ----------
def _profile_out(user: User, prof) -> ProfileOut:
    return ProfileOut(
        user_id=user.id,
        phone=user.phone,
        email=user.email,
        email_verified=user.email_verified_at is not None,
        full_name=user.full_name,
        display_name=prof.display_name,
        avatar_url=media.resolve(prof.avatar_url),
        date_of_birth=prof.date_of_birth,
        gender=prof.gender,
        locale=prof.locale,
        alternate_phone=prof.alternate_phone,
        gstin=prof.gstin,
        legal_name=prof.legal_name,
        notify_whatsapp=prof.notify_whatsapp,
        notify_sms=prof.notify_sms,
        notify_push=prof.notify_push,
        notify_email=prof.notify_email,
        marketing_opt_in=prof.marketing_opt_in,
        quiet_hours_start=prof.quiet_hours_start,
        quiet_hours_end=prof.quiet_hours_end,
    )


@router.get("/profile", response_model=ProfileOut, operation_id="get_profile")
async def get_profile(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    prof = await svc.get_or_create_profile(db, user)
    await db.commit()
    return _profile_out(user, prof)


@router.patch("/profile", response_model=ProfileOut, operation_id="update_profile")
async def update_profile(
    data: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prof = await svc.get_or_create_profile(db, user)
    patch = data.model_dump(exclude_unset=True)

    if media.is_ref(patch.get("avatar_url")) and not media.ref_owned(
        patch["avatar_url"], "uploads", user.id
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown avatar upload")
    if patch.get("date_of_birth") and patch["date_of_birth"] > svc.today_ist():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_of_birth is in the future")

    # Fields that live on the User row.
    if "email" in patch:
        new_email = (patch.pop("email") or "").strip().lower() or None
        if new_email != user.email:
            # Not unique while unverified: claiming an address proves nothing, so it must
            # neither lock the real owner out nor reveal that the address is registered.
            user.email = new_email
            user.email_verified_at = None  # a changed address must be re-verified
    if "full_name" in patch:
        user.full_name = patch.pop("full_name")
    if "notify_whatsapp" in patch and patch["notify_whatsapp"] != prof.notify_whatsapp:
        # Turning WhatsApp on/off IS the user's opt-in/out (Meta policy): record it in the
        # consent ledger so the toggle and the ledger can't disagree.
        await svc.record_consent(
            db, user.id, "whatsapp_optin",
            "granted" if patch["notify_whatsapp"] else "withdrawn", ip=None, ua=None,
        )
    for field, value in patch.items():
        setattr(prof, field, value)

    await db.commit()
    await db.refresh(user)
    await db.refresh(prof)
    return _profile_out(user, prof)


# ---------- Email ownership verification ----------
class EmailVerifyRequestOut(BaseModel):
    detail: str
    dev_token: str | None = None  # non-prod only (no real mail sent), like dev_otp


class EmailVerifyIn(BaseModel):
    token: str = Field(min_length=10, max_length=512)


# Marketing consent per channel. WhatsApp MARKETING has its own purpose: the
# whatsapp_optin above only enables the channel (order updates) — opting in to those must
# never also opt the user in to promotions.
_MARKETING_PURPOSES = ("marketing_sms", "marketing_email", "marketing_push", "marketing_whatsapp")


@router.post("/account/email/verify-request", response_model=EmailVerifyRequestOut,
             operation_id="request_email_verification")
async def request_email_verification(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if not user.email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Set an email first")
    if user.email_verified_at is not None:
        return {"detail": "email already verified"}
    if not await allow_send("email_verify", user.email):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "A code was sent recently; try again later"
        )
    token = email_verify.make_token(user.id, user.email)
    sent, _msg_id, _err = await senders.send_email(
        user.email, f"Confirm your DRS email with this code: {token}",
        {"subject": "Confirm your email"},
    )
    if not sent and settings.ENV == "prod":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email could not be sent")
    return {
        "detail": "verification sent",
        "dev_token": token if settings.ENV != "prod" else None,
    }


@router.post("/account/email/verify", response_model=ProfileOut, operation_id="verify_email")
async def verify_email(
    data: EmailVerifyIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if not email_verify.check_token(data.token, user.id, user.email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired verification code")
    # An address is unique only among VERIFIED owners (an unverified claim can't lock the
    # real owner out); the partial unique index backstops this check.
    taken = await db.scalar(
        select(func.count()).select_from(User).where(
            func.lower(User.email) == user.email.lower(),
            User.email_verified_at.is_not(None),
            User.id != user.id,
        )
    )
    if taken:
        raise HTTPException(status.HTTP_409_CONFLICT, "This email is verified on another account")
    user.email_verified_at = dt.datetime.now(dt.UTC)
    prof = await svc.get_or_create_profile(db, user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This email is verified on another account"
        ) from None
    await db.refresh(user)
    return _profile_out(user, prof)


# ---------- DPDP data rights ----------
@router.get("/account/export", operation_id="export_my_data", response_model=DataExportOut)
async def export_my_data(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Data access + portability: everything we hold about the caller."""
    return await dpdp.export_user_data(db, user)


@router.post("/account/erase", operation_id="erase_my_account", response_model=EraseResultOut)
async def erase_my_account(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Right to erasure — anonymizes PII; tax/financial records are retained by law."""
    try:
        return await dpdp.erase_user(db, user)
    except dpdp.ErasureBlocked as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


# ---------- Consent ----------
@router.get("/consents", operation_id="get_consents", response_model=dict[str, str])
async def get_consents(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await svc.current_consents(db, user.id)


@router.post("/consents", response_model=ConsentOut, operation_id="record_consent")
async def post_consent(
    data: ConsentIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    meta = _client(request)
    c = await svc.record_consent(
        db, user.id, data.purpose, data.action, ip=meta["ip"], ua=meta["ua"]
    )
    prof = await svc.get_or_create_profile(db, user)
    granted = data.action == "granted"
    # Marketing consent gates MARKETING sends only (via marketing_opt_in + the recorded
    # consent). It must NOT flip the per-channel notify_sms/notify_email toggles — those
    # also gate TRANSACTIONAL messages (OTP, order/payment updates), so revoking marketing
    # SMS/email must never silence a user's transactional SMS/email (#35).
    if data.purpose == "whatsapp_optin":
        # WhatsApp is different: the business may message the user on WhatsApp at all only
        # with an opt-in (Meta policy), so this toggle genuinely enables/disables the channel.
        prof.notify_whatsapp = granted
    if data.purpose in _MARKETING_PURPOSES:
        # Derive the marketing master from ALL marketing consents (ON if ANY is granted),
        # not last-write-wins — otherwise revoking one marketing channel would silently
        # mute the others. The per-purpose consent still gates each channel individually.
        consents = await svc.current_consents(db, user.id)
        consents[data.purpose] = data.action  # reflect the action just recorded
        prof.marketing_opt_in = any(
            consents.get(p) == "granted" for p in _MARKETING_PURPOSES
        )
    await db.commit()
    await db.refresh(c)
    return c


# ---------- Addresses ----------
@router.get("/addresses", response_model=list[AddressOut], operation_id="list_addresses")
async def list_addresses(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    ids = await svc.my_account_ids(db, user)
    if not ids:
        return []
    res = await db.execute(
        select(Address)
        .where(Address.account_id.in_(ids), Address.status == "active")
        .order_by(Address.is_default.desc(), Address.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    return res.scalars().all()


@router.post(
    "/addresses",
    response_model=AddressOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_address",
)
async def create_address(
    data: AddressIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await svc.get_or_create_account(db, user)
    addr = await svc.create_address(db, account.id, data)
    await db.commit()
    await db.refresh(addr)
    return addr


@router.patch("/addresses/{address_id}", response_model=AddressOut, operation_id="update_address")
async def update_address(
    address_id: int,
    data: AddressUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    addr = await _owned_address(db, user, address_id)
    addr = await svc.update_address(db, addr, data)
    await db.commit()
    await db.refresh(addr)
    return addr


@router.post(
    "/addresses/{address_id}/default", response_model=AddressOut, operation_id="set_default_address"
)
async def set_default_address(
    address_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    addr = await _owned_address(db, user, address_id)
    await svc.update_address(db, addr, AddressUpdate(is_default=True))
    await db.commit()
    await db.refresh(addr)
    return addr


@router.delete(
    "/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_address",
)
async def delete_address(
    address_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    addr = await _owned_address(db, user, address_id)
    await svc.delete_address(db, addr)
    await db.commit()


# ---------- Payment methods ----------
@router.get(
    "/payment-methods", response_model=list[PaymentMethodOut], operation_id="list_payment_methods"
)
async def list_payment_methods(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    ids = await svc.my_account_ids(db, user)
    if not ids:
        return []
    res = await db.execute(
        select(PaymentMethod)
        .where(PaymentMethod.account_id.in_(ids), PaymentMethod.status == "active")
        .order_by(PaymentMethod.is_default.desc(), PaymentMethod.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    return res.scalars().all()


@router.post(
    "/payment-methods",
    response_model=PaymentMethodOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_payment_method",
)
async def create_payment_method(
    data: PaymentMethodIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await svc.get_or_create_account(db, user)
    pm = await svc.create_payment_method(db, account.id, data)
    await db.commit()
    await db.refresh(pm)
    return pm


@router.post(
    "/payment-methods/{pm_id}/default",
    response_model=PaymentMethodOut,
    operation_id="set_default_payment_method",
)
async def set_default_payment_method(
    pm_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pm = await _owned_method(db, user, pm_id)
    await svc.set_default_method(db, pm)
    await db.commit()
    await db.refresh(pm)
    return pm


@router.delete(
    "/payment-methods/{pm_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_payment_method",
)
async def delete_payment_method(
    pm_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pm = await _owned_method(db, user, pm_id)
    await svc.delete_payment_method(db, pm)
    await db.commit()


# ---------- Mandates (autopay) ----------
@router.get("/mandates", response_model=list[MandateOut], operation_id="list_mandates")
async def list_mandates(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    ids = await svc.my_account_ids(db, user)
    if not ids:
        return []
    res = await db.execute(
        select(PaymentMandate)
        .where(PaymentMandate.account_id.in_(ids))
        .order_by(PaymentMandate.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    return res.scalars().all()


@router.post(
    "/mandates",
    response_model=MandateOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_mandate",
)
async def create_mandate(
    data: MandateIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await svc.get_or_create_account(db, user)
    if data.payment_method_id is not None:
        await _owned_method(db, user, data.payment_method_id)
    if data.valid_from and data.valid_to and data.valid_to < data.valid_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid_to before valid_from")
    if data.valid_to and data.valid_to < svc.today_ist():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid_to is in the past")
    # RBI caps a UPI AutoPay / e-NACH per-debit at ₹15,000 (above it needs pre-debit auth).
    if float(data.max_amount) > 15000:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "mandate max_amount cannot exceed ₹15,000 per debit (RBI UPI AutoPay limit)",
        )
    mandate = PaymentMandate(
        account_id=account.id,
        payment_method_id=data.payment_method_id,
        max_amount=data.max_amount,
        frequency=data.frequency,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        status="created",
    )
    db.add(mandate)
    await db.commit()
    await db.refresh(mandate)
    return mandate


@router.post("/mandates/{mandate_id}/cancel", response_model=MandateOut, operation_id="cancel_mandate")
async def cancel_mandate(
    mandate_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The customer revokes an autopay mandate (they must always be able to)."""
    mandate = await db.get(PaymentMandate, mandate_id)
    if mandate is None or mandate.account_id not in await svc.my_account_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mandate not found")
    if mandate.status != "cancelled":
        mandate.status = "cancelled"
        await db.commit()
        await db.refresh(mandate)
    return mandate

