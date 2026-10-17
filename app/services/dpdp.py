"""DPDP data-subject rights: access/portability (export) and erasure.

Erasure is PII tombstoning, not hard-delete: financial/tax records (invoices,
payments, ledgers) must be retained for legal compliance, so we anonymize the
person while leaving the money trail intact, and revoke all consents.
"""
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device_token import DeviceToken
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.profile import Address, Consent, PaymentMandate, PaymentMethod, UserProfile
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import profile as profile_svc


def _b36(n: int) -> str:
    """Compact base-36 so an erasure tombstone fits phone's VARCHAR(16) for any id."""
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


async def export_user_data(db: AsyncSession, user: User) -> dict:
    prof = await db.get(UserProfile, user.id)
    account_ids = await profile_svc.my_account_ids(db, user)

    addresses = (
        await db.execute(select(Address).where(Address.account_id.in_(account_ids)))
    ).scalars().all() if account_ids else []
    methods = (
        await db.execute(select(PaymentMethod).where(PaymentMethod.account_id.in_(account_ids)))
    ).scalars().all() if account_ids else []
    mandates = (
        await db.execute(select(PaymentMandate).where(PaymentMandate.account_id.in_(account_ids)))
    ).scalars().all() if account_ids else []
    device_tokens = (
        await db.execute(select(DeviceToken).where(DeviceToken.user_id == user.id))
    ).scalars().all()
    orders = (
        await db.execute(select(Order).where(Order.account_id.in_(account_ids)))
    ).scalars().all() if account_ids else []
    invoices = (
        await db.execute(select(Invoice).where(Invoice.account_id.in_(account_ids)))
    ).scalars().all() if account_ids else []
    consents = (
        await db.execute(select(Consent).where(Consent.user_id == user.id).order_by(Consent.id))
    ).scalars().all()

    return {
        "user": {"id": user.id, "phone": user.phone, "email": user.email, "name": user.full_name},
        "profile": {
            "display_name": prof.display_name,
            "gstin": prof.gstin,
            "legal_name": prof.legal_name,
            "locale": prof.locale,
        }
        if prof
        else None,
        "addresses": [
            {"id": a.id, "line1": a.line1, "city": a.city, "state": a.state, "pincode": a.pincode}
            for a in addresses
        ],
        "payment_methods": [
            {"id": m.id, "type": m.method_type, "display": m.display, "brand": m.brand}
            for m in methods
        ],  # masked display only — no card/UPI credentials are stored
        "payment_mandates": [
            {
                "id": m.id, "gateway": m.gateway, "max_amount": float(m.max_amount),
                "frequency": m.frequency, "status": m.status,
                "valid_from": str(m.valid_from) if m.valid_from else None,
                "valid_to": str(m.valid_to) if m.valid_to else None,
            }
            for m in mandates
        ],
        "device_tokens": [
            {"id": d.id, "platform": getattr(d, "platform", None), "active": d.active}
            for d in device_tokens
        ],
        "orders": [{"id": o.id, "order_no": o.order_no, "total": float(o.total)} for o in orders],
        "invoices": [
            {"invoice_no": i.invoice_no, "total": float(i.total), "date": str(i.invoice_date)}
            for i in invoices
        ],
        "consents": [
            {"purpose": c.purpose, "action": c.action, "at": str(c.created_at)} for c in consents
        ],
    }


async def erase_user(db: AsyncSession, user: User) -> dict:
    """Anonymize PII and revoke consent; retain financial records (append a withdrawal)."""
    # base-36 keeps the unique tombstone within phone's VARCHAR(16) for any id size.
    user.phone = f"erz-{_b36(user.id)}"
    user.email = None
    user.full_name = None
    user.status = "erased"
    user.token_version += 1  # invalidate all existing tokens

    prof = await db.get(UserProfile, user.id)
    if prof is not None:
        prof.display_name = None
        prof.avatar_url = None
        prof.date_of_birth = None
        prof.gender = None
        prof.alternate_phone = None
        prof.gstin = None
        prof.legal_name = None
        prof.marketing_opt_in = False
        prof.notify_whatsapp = prof.notify_sms = prof.notify_push = prof.notify_email = False

    account_ids = await profile_svc.my_account_ids(db, user)
    if account_ids:
        addresses = (
            await db.execute(select(Address).where(Address.account_id.in_(account_ids)))
        ).scalars().all()
        for a in addresses:
            a.contact_name = None
            a.contact_phone = None
            # Overwrite the physical-address PII too (line1/city/state/pincode are NOT NULL,
            # so they get a tombstone rather than NULL) and drop the GPS point.
            a.line1 = "erased"
            a.line2 = None
            a.landmark = None
            a.city = "erased"
            a.state = "erased"
            a.pincode = "000000"
            a.geo = None
            a.status = "archived"
        methods = (
            await db.execute(select(PaymentMethod).where(PaymentMethod.account_id.in_(account_ids)))
        ).scalars().all()
        for m in methods:
            m.gateway_token = None
            m.gateway_customer_id = None
            m.display = None
            m.status = "removed"

    # Deactivate device tokens so an erased subject stops receiving push.
    await db.execute(
        update(DeviceToken).where(DeviceToken.user_id == user.id).values(active=False)
    )

    for purpose in ("whatsapp_optin", "marketing_sms", "marketing_email", "data_processing"):
        db.add(Consent(user_id=user.id, purpose=purpose, action="withdrawn", source="erasure"))

    await db.commit()
    return {"detail": "account erased", "user_id": user.id, "retained": "financial/tax records"}


async def owner_user_for_account(db: AsyncSession, account_id: int) -> User | None:
    account = await db.get(CustomerAccount, account_id)
    return await db.get(User, account.owner_user_id) if account else None
