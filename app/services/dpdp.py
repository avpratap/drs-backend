"""DPDP data-subject rights: access/portability (export) and erasure.

Erasure is PII tombstoning, not hard-delete: financial/tax records (invoices,
payments, ledgers) must be retained for legal compliance, so we anonymize the
person while leaving the money trail intact, and revoke all consents.
"""
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_session import AuthSession
from app.models.delivery import DeliveryOrder
from app.models.device_token import DeviceToken
from app.models.driver import DriverLocation
from app.models.fulfillment import ProofOfDelivery
from app.models.invoice import Invoice
from app.models.newsletter import NewsletterSubscriber
from app.models.notification import Notification
from app.models.order import Order
from app.models.profile import Address, Consent, PaymentMandate, PaymentMethod, UserProfile
from app.models.rating import Rating
from app.models.shipment import Shipment
from app.models.shop import CustomerAccount
from app.models.support import SupportTicket, TicketMessage
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
    tickets = (
        await db.execute(select(SupportTicket).where(SupportTicket.created_by == user.id))
    ).scalars().all()
    ratings = (
        await db.execute(select(Rating).where(Rating.user_id == user.id))
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
        "support_tickets": [
            {"id": t.id, "subject": t.subject, "status": t.status} for t in tickets
        ],
        "ratings": [
            {"product_id": r.product_id, "stars": r.stars, "comment": r.comment} for r in ratings
        ],
    }


async def erase_user(db: AsyncSession, user: User) -> dict:
    """Anonymize PII and revoke consent; retain financial/tax records (invoices, ledgers).
    Covers every PII-bearing table keyed to the user or their account(s)."""
    orig_email = user.email  # capture before nulling (newsletter is keyed by email)
    # base-36 keeps the unique tombstone within phone's VARCHAR(16) for any id size.
    user.phone = f"erz-{_b36(user.id)}"
    user.email = None
    user.full_name = None
    user.password_hash = None
    user.phone_verified = False
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

        # Cancel UPI-AutoPay / e-NACH mandates so an erased subject can't still be debited.
        await db.execute(
            update(PaymentMandate)
            .where(
                PaymentMandate.account_id.in_(account_ids),
                PaymentMandate.status.notin_(("cancelled", "expired")),
            )
            .values(status="cancelled", gateway_mandate_id=None)
        )
        # Proof-of-delivery (recipient name, signature/photo, GPS) via the account's orders.
        do_ids = (
            await db.execute(
                select(DeliveryOrder.id).where(DeliveryOrder.account_id.in_(account_ids))
            )
        ).scalars().all()
        if do_ids:
            await db.execute(
                update(ProofOfDelivery)
                .where(ProofOfDelivery.delivery_order_id.in_(do_ids))
                .values(
                    recipient_name=None, photo_url=None, signature_url=None,
                    note=None, lat=None, lng=None,
                )
            )
        # Courier shipment destination snapshot (keep state/pincode for tax/courier stats).
        await db.execute(
            update(Shipment)
            .where(Shipment.account_id.in_(account_ids))
            .values(to_name=None, to_phone=None, to_address=None, to_city=None)
        )

    # Deactivate device tokens so an erased subject stops receiving push.
    await db.execute(
        update(DeviceToken).where(DeviceToken.user_id == user.id).values(active=False)
    )
    # Scrub rendered PII from the notification outbox (to_addr = phone/token, body/payload).
    await db.execute(
        update(Notification).where(Notification.user_id == user.id).values(
            to_addr=None, body=None, payload=None
        )
    )
    # Support tickets / messages (free-text often contains name/address/phone).
    await db.execute(
        update(SupportTicket).where(SupportTicket.created_by == user.id).values(subject="erased")
    )
    await db.execute(
        update(TicketMessage).where(TicketMessage.sender_user_id == user.id).values(
            body="erased", attachment_url=None
        )
    )
    # Review free-text.
    await db.execute(
        update(Rating).where(Rating.user_id == user.id).values(comment=None, admin_reply=None)
    )
    # Auth-session request metadata (IP / user-agent / device name).
    await db.execute(
        update(AuthSession).where(AuthSession.user_id == user.id).values(
            ip=None, user_agent=None, device_name=None
        )
    )
    # Driver GPS ping history (if the subject was a delivery user).
    await db.execute(delete(DriverLocation).where(DriverLocation.driver_id == user.id))
    # Newsletter subscription (keyed by email, not user_id) — fully remove.
    if orig_email:
        await db.execute(
            delete(NewsletterSubscriber).where(
                func.lower(NewsletterSubscriber.email) == orig_email.lower()
            )
        )

    for purpose in ("whatsapp_optin", "marketing_sms", "marketing_email", "data_processing"):
        db.add(Consent(user_id=user.id, purpose=purpose, action="withdrawn", source="erasure"))

    await db.commit()
    return {"detail": "account erased", "user_id": user.id, "retained": "financial/tax records"}


async def owner_user_for_account(db: AsyncSession, account_id: int) -> User | None:
    account = await db.get(CustomerAccount, account_id)
    return await db.get(User, account.owner_user_id) if account else None
