from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.models.profile import Address, PaymentMandate, PaymentMethod
from app.models.user import User
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
from app.services import profile as svc

router = APIRouter(tags=["account"])


def _client(request: Request) -> dict:
    return {
        "ip": request.client.host if request.client else None,
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
        full_name=user.full_name,
        display_name=prof.display_name,
        avatar_url=prof.avatar_url,
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

    if patch.get("date_of_birth") and patch["date_of_birth"] > svc.today_ist():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_of_birth is in the future")

    # Fields that live on the User row.
    if "email" in patch:
        user.email = patch.pop("email")
    if "full_name" in patch:
        user.full_name = patch.pop("full_name")
    for field, value in patch.items():
        setattr(prof, field, value)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use") from None
    await db.refresh(user)
    await db.refresh(prof)
    return _profile_out(user, prof)


# ---------- Consent ----------
@router.get("/consents", operation_id="get_consents")
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
    # Reflect WhatsApp/SMS/email marketing opt-in on the profile toggle.
    prof = await svc.get_or_create_profile(db, user)
    granted = data.action == "granted"
    if data.purpose == "whatsapp_optin":
        prof.notify_whatsapp = granted
    elif data.purpose == "marketing_sms":
        prof.notify_sms = granted
    elif data.purpose == "marketing_email":
        prof.notify_email = granted
    if data.purpose in ("marketing_sms", "marketing_email", "whatsapp_optin"):
        prof.marketing_opt_in = granted
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
