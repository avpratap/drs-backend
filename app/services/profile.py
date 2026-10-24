"""Profile / address book / payment method / consent service.

Invariants enforced here (the DB partial-unique index is the backstop):
- Setting an address or payment method as default atomically unsets the prior
  default in the same account, within one transaction, before flushing.
- The first active address/method for an account becomes default automatically.
- Default-flips take pg_advisory_xact_lock on the account id so two concurrent
  "make default" calls can't both win and violate the unique index.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from geoalchemy2 import WKTElement
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.profile import (
    Address,
    Consent,
    PaymentMandate,
    PaymentMethod,
    UserProfile,
)
from app.models.shop import CustomerAccount
from app.models.user import User

_ADDR_LOCK_NS = 515151
_PM_LOCK_NS = 515152


def today_ist() -> dt.date:
    return dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date()


def account_type_for(user: User, *, business: bool = False) -> str:
    """ONE rule for a new account's type, whichever endpoint creates it first: a
    subscriber's account is B2B (as is one created by registering a shop)."""
    return "b2b" if business or user.role == "subscriber" else "retail"


async def get_or_create_account(db: AsyncSession, user: User) -> CustomerAccount:
    """The caller's primary billing account (lowest id). Created lazily."""
    res = await db.execute(
        select(CustomerAccount)
        .where(CustomerAccount.owner_user_id == user.id)
        .order_by(CustomerAccount.id)
    )
    account = res.scalars().first()
    if account is None:
        account = CustomerAccount(
            owner_user_id=user.id,
            account_type=account_type_for(user),
            billing_mode="prepaid_wallet",
        )
        db.add(account)
        await db.flush()
    return account


async def my_account_ids(db: AsyncSession, user: User) -> set[int]:
    res = await db.execute(
        select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id)
    )
    return set(res.scalars().all())


async def get_or_create_profile(
    db: AsyncSession, user: User, *, fresh: bool = False
) -> UserProfile:
    """`fresh` re-reads the row (a long-lived session, e.g. the notification drain, must
    see a consent/channel change committed since it last loaded the profile)."""
    prof = await db.get(UserProfile, user.id, populate_existing=fresh)
    if prof is None:
        # Race-safe create: a concurrent first touch may insert the same user_id.
        try:
            async with db.begin_nested():
                prof = UserProfile(user_id=user.id)
                db.add(prof)
                await db.flush()
        except IntegrityError:
            prof = await db.get(UserProfile, user.id, populate_existing=True)
    return prof


# ---------- Addresses ----------
def _geo(lat: float | None, lng: float | None):
    if lat is None or lng is None:
        return None
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


async def _lock_account(db: AsyncSession, ns: int, account_id: int) -> None:
    await db.execute(select(func.pg_advisory_xact_lock(ns, account_id)))


async def _clear_default_addresses(db: AsyncSession, account_id: int) -> None:
    await db.execute(
        update(Address)
        .where(Address.account_id == account_id, Address.is_default.is_(True))
        .values(is_default=False)
    )


async def create_address(db: AsyncSession, account_id: int, data) -> Address:
    await _lock_account(db, _ADDR_LOCK_NS, account_id)
    # First active address is forced default; otherwise honour the request flag.
    existing = await db.scalar(
        select(func.count())
        .select_from(Address)
        .where(Address.account_id == account_id, Address.status == "active")
    )
    make_default = data.is_default or existing == 0
    if make_default:
        await _clear_default_addresses(db, account_id)
    addr = Address(
        account_id=account_id,
        label=data.label,
        contact_name=data.contact_name,
        contact_phone=data.contact_phone,
        line1=data.line1,
        line2=data.line2,
        landmark=data.landmark,
        city=data.city,
        state=data.state,
        pincode=data.pincode,
        country=data.country,
        geo=_geo(data.lat, data.lng),
        is_default=make_default,
        status="active",
    )
    db.add(addr)
    await db.flush()
    return addr


async def _still_active(db: AsyncSession, row, what: str) -> None:
    """Re-read a row AFTER taking the account lock: the caller's ownership/status check ran
    before it, and a concurrent delete may have archived the row meanwhile (e.g. making
    an archived address the default, leaving the account with no active default)."""
    await db.refresh(row)
    if row.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


async def update_address(db: AsyncSession, addr: Address, data) -> Address:
    await _lock_account(db, _ADDR_LOCK_NS, addr.account_id)
    await _still_active(db, addr, "Address")
    patch = data.model_dump(exclude_unset=True)
    lat, lng = patch.pop("lat", None), patch.pop("lng", None)
    want_default = patch.pop("is_default", None)

    for field, value in patch.items():
        setattr(addr, field, value)
    if "lat" in data.model_fields_set or "lng" in data.model_fields_set:
        addr.geo = _geo(lat, lng)

    if want_default is True:
        await _clear_default_addresses(db, addr.account_id)
        addr.is_default = True
    elif want_default is False and addr.is_default:
        # Unsetting the default would leave the account with none (invoices then fall
        # back to the seller state for place of supply): make another address default.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Make another address the default instead of unsetting this one",
        )
    await db.flush()
    return addr


async def delete_address(db: AsyncSession, addr: Address) -> None:
    await _lock_account(db, _ADDR_LOCK_NS, addr.account_id)
    await _still_active(db, addr, "Address")
    was_default = addr.is_default
    addr.status = "archived"
    addr.is_default = False
    await db.flush()
    if was_default:
        # Promote the most-recently-created active address to default.
        nxt = await db.scalar(
            select(Address)
            .where(Address.account_id == addr.account_id, Address.status == "active")
            .order_by(Address.id.desc())
            .limit(1)
        )
        if nxt is not None:
            nxt.is_default = True
            await db.flush()


# ---------- Payment methods ----------
async def _clear_default_methods(db: AsyncSession, account_id: int) -> None:
    await db.execute(
        update(PaymentMethod)
        .where(PaymentMethod.account_id == account_id, PaymentMethod.is_default.is_(True))
        .values(is_default=False)
    )


async def create_payment_method(db: AsyncSession, account_id: int, data) -> PaymentMethod:
    await _lock_account(db, _PM_LOCK_NS, account_id)
    existing = await db.scalar(
        select(func.count())
        .select_from(PaymentMethod)
        .where(PaymentMethod.account_id == account_id, PaymentMethod.status == "active")
    )
    make_default = data.is_default or existing == 0
    if make_default:
        await _clear_default_methods(db, account_id)
    pm = PaymentMethod(
        account_id=account_id,
        method_type=data.method_type,
        gateway_customer_id=data.gateway_customer_id,
        gateway_token=data.gateway_token,
        display=data.display,
        brand=data.brand,
        expiry_month=data.expiry_month,
        expiry_year=data.expiry_year,
        is_default=make_default,
        status="active",
    )
    db.add(pm)
    await db.flush()
    return pm


async def set_default_method(db: AsyncSession, pm: PaymentMethod) -> None:
    await _lock_account(db, _PM_LOCK_NS, pm.account_id)
    await _still_active(db, pm, "Payment method")
    await _clear_default_methods(db, pm.account_id)
    pm.is_default = True
    await db.flush()


async def delete_payment_method(db: AsyncSession, pm: PaymentMethod) -> None:
    await _lock_account(db, _PM_LOCK_NS, pm.account_id)
    await _still_active(db, pm, "Payment method")
    was_default = pm.is_default
    pm.status = "removed"
    pm.is_default = False
    # A removed instrument must not keep live autopay mandates debiting it.
    await db.execute(
        update(PaymentMandate)
        .where(PaymentMandate.payment_method_id == pm.id, PaymentMandate.status != "cancelled")
        .values(status="cancelled")
    )
    await db.flush()
    if was_default:
        nxt = await db.scalar(
            select(PaymentMethod)
            .where(PaymentMethod.account_id == pm.account_id, PaymentMethod.status == "active")
            .order_by(PaymentMethod.id.desc())
            .limit(1)
        )
        if nxt is not None:
            nxt.is_default = True
            await db.flush()


# ---------- Consent ----------
async def record_consent(
    db: AsyncSession, user_id: int, purpose: str, action: str, *, source="app", ip=None, ua=None
) -> Consent:
    c = Consent(
        user_id=user_id, purpose=purpose, action=action, source=source, ip=ip, user_agent=ua
    )
    db.add(c)
    await db.flush()
    return c


async def current_consents(db: AsyncSession, user_id: int) -> dict[str, str]:
    """Latest action per purpose (append-only ledger -> current state)."""
    res = await db.execute(
        select(Consent)
        .where(Consent.user_id == user_id)
        .order_by(Consent.purpose, Consent.id.desc())
    )
    state: dict[str, str] = {}
    for c in res.scalars().all():
        state.setdefault(c.purpose, c.action)  # first seen per purpose = latest
    return state
