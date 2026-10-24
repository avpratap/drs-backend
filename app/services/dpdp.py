"""DPDP data-subject rights: access/portability (export) and erasure.

Erasure is PII tombstoning, not hard-delete: financial/tax records (invoices,
payments, ledgers) must be retained for legal compliance, so we anonymize the
person while leaving the money trail intact, and revoke all consents.
"""
import datetime as dt
from decimal import Decimal

from sqlalchemy import delete, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_session import AuthSession
from app.models.billing import CreditLedger, Wallet, WalletTransaction, WithdrawalRequest
from app.models.cart import Cart, CartItem
from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.device_token import DeviceToken
from app.models.driver import DriverLocation
from app.models.fulfillment import ProofOfDelivery
from app.models.invoice import Invoice
from app.models.loyalty import LoyaltyAccount, PointsLedger
from app.models.newsletter import NewsletterSubscriber
from app.models.notification import Notification
from app.models.order import Order, OrderItem
from app.models.payment import Payment
from app.models.profile import Address, Consent, PaymentMandate, PaymentMethod, UserProfile
from app.models.rating import Rating
from app.models.refund import Refund
from app.models.rma import ReturnRequest, ReturnRequestItem
from app.models.shipment import Shipment
from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
from app.models.subscription import Subscription, SubscriptionItem
from app.models.support import SupportTicket, TicketMessage
from app.models.user import User
from app.models.wishlist import WishlistItem
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


# Columns never exported: credentials / secrets / internal hashes.
_EXPORT_SECRET_COLS = {
    "password_hash", "token_version", "refresh_hash", "gateway_token", "gateway_customer_id",
    "gateway_mandate_id", "confirm_token",
}


def _row(obj, *, mask: tuple[str, ...] = ()) -> dict:
    """Every column of an ORM row as JSON-safe values (generic, so a new PII column is
    exported automatically instead of silently missing from a hand-written field list)."""
    out: dict = {}
    for col in obj.__table__.columns:
        if col.key in _EXPORT_SECRET_COLS:
            continue
        v = getattr(obj, col.key, None)
        if col.key in mask and v:
            v = f"***{str(v)[-6:]}"
        elif isinstance(v, (dt.date, dt.datetime)):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = float(v)
        elif v is not None and not isinstance(v, (str, int, float, bool, dict, list)):
            v = str(v)  # e.g. a PostGIS geo point (WKB hex)
        out[col.key] = v
    return out


async def _rows(db: AsyncSession, model, *where, order=None, limit=None, **kw) -> list[dict]:
    q = select(model).where(*where).order_by(order if order is not None else model.id)
    if limit is not None:
        q = q.limit(limit)
    return [_row(r, **kw) for r in (await db.execute(q)).scalars().all()]


# GPS pings accumulate every few seconds while a driver is on shift: export the most
# recent ones (bounded response) and say how many exist in total.
_DRIVER_PINGS_EXPORT_CAP = 10_000


async def export_user_data(db: AsyncSession, user: User) -> dict:
    """Data access + portability: EVERY PII-bearing row held about the subject (their user
    row and everything keyed to their user id or their account(s))."""
    prof = await db.get(UserProfile, user.id)
    acc = list(await profile_svc.my_account_ids(db, user))

    def by_acc(model):
        return model.account_id.in_(acc) if acc else false()

    order_ids = select(Order.id).where(by_acc(Order))
    do_ids = select(DeliveryOrder.id).where(by_acc(DeliveryOrder))
    shop_ids = select(Shop.id).where(by_acc(Shop))
    sub_ids = select(Subscription.id).where(by_acc(Subscription))
    rr_ids = select(ReturnRequest.id).where(by_acc(ReturnRequest))
    ticket_ids = select(SupportTicket.id).where(SupportTicket.created_by == user.id)
    wallet_ids = select(Wallet.id).where(by_acc(Wallet))
    cart_ids = select(Cart.id).where(by_acc(Cart))

    newsletter = []
    # Only a VERIFIED address proves the newsletter row is this user's (anyone can type
    # someone else's email into their profile).
    if user.email and user.email_verified_at is not None:
        newsletter = await _rows(
            db, NewsletterSubscriber, func.lower(NewsletterSubscriber.email) == user.email.lower()
        )

    return {
        "user": _row(user),
        "profile": _row(prof) if prof else None,
        "accounts": await _rows(db, CustomerAccount, CustomerAccount.owner_user_id == user.id),
        "shops": await _rows(db, Shop, by_acc(Shop)),
        "addresses": await _rows(db, Address, by_acc(Address)),
        "payment_methods": await _rows(db, PaymentMethod, by_acc(PaymentMethod)),
        "payment_mandates": await _rows(db, PaymentMandate, by_acc(PaymentMandate)),
        "device_tokens": await _rows(db, DeviceToken, DeviceToken.user_id == user.id, mask=("token",)),
        "subscriptions": await _rows(db, Subscription, by_acc(Subscription)),
        "subscription_items": await _rows(db, SubscriptionItem, SubscriptionItem.subscription_id.in_(sub_ids)),
        "orders": await _rows(db, Order, by_acc(Order)),
        "order_items": await _rows(db, OrderItem, OrderItem.order_id.in_(order_ids)),
        "delivery_orders": await _rows(db, DeliveryOrder, by_acc(DeliveryOrder)),
        "delivery_order_items": await _rows(
            db, DeliveryOrderItem, DeliveryOrderItem.delivery_order_id.in_(do_ids)
        ),
        "proofs_of_delivery": await _rows(
            db, ProofOfDelivery, ProofOfDelivery.delivery_order_id.in_(do_ids)
        ),
        "shipments": await _rows(db, Shipment, by_acc(Shipment)),
        "payments": await _rows(db, Payment, by_acc(Payment)),
        "refunds": await _rows(db, Refund, by_acc(Refund)),
        "invoices": await _rows(db, Invoice, by_acc(Invoice)),
        "returns": await _rows(db, ReturnRequest, by_acc(ReturnRequest)),
        "return_items": await _rows(
            db, ReturnRequestItem, ReturnRequestItem.return_request_id.in_(rr_ids)
        ),
        "wallet": await _rows(db, Wallet, by_acc(Wallet)),
        "wallet_transactions": await _rows(
            db, WalletTransaction, WalletTransaction.wallet_id.in_(wallet_ids)
        ),
        "credit_ledger": await _rows(db, CreditLedger, by_acc(CreditLedger)),
        "withdrawals": await _rows(db, WithdrawalRequest, by_acc(WithdrawalRequest)),
        "loyalty": await _rows(db, LoyaltyAccount, by_acc(LoyaltyAccount)),
        "points_ledger": await _rows(db, PointsLedger, by_acc(PointsLedger)),
        "cart_items": await _rows(db, CartItem, CartItem.cart_id.in_(cart_ids)),
        "wishlist": await _rows(db, WishlistItem, by_acc(WishlistItem)),
        "consents": await _rows(db, Consent, Consent.user_id == user.id),
        "support_tickets": await _rows(db, SupportTicket, SupportTicket.created_by == user.id),
        "ticket_messages": await _rows(
            db, TicketMessage,
            or_(TicketMessage.sender_user_id == user.id, TicketMessage.ticket_id.in_(ticket_ids)),
        ),
        "ratings": await _rows(db, Rating, Rating.user_id == user.id),
        "notifications": await _rows(db, Notification, Notification.user_id == user.id),
        "auth_sessions": await _rows(db, AuthSession, AuthSession.user_id == user.id),
        "driver_locations": await _rows(
            db, DriverLocation, DriverLocation.driver_id == user.id,
            order=DriverLocation.id.desc(), limit=_DRIVER_PINGS_EXPORT_CAP,
        ),
        "driver_locations_total": await db.scalar(
            select(func.count()).select_from(DriverLocation)
            .where(DriverLocation.driver_id == user.id)
        ),
        "newsletter": newsletter,
        "shop_ids": [i for (i,) in (await db.execute(shop_ids)).all()],
    }


class ErasureBlocked(Exception):
    """Erasure must wait (an open money flow would be orphaned)."""


async def erase_user(db: AsyncSession, user: User) -> dict:
    """Anonymize PII and revoke consent; retain financial/tax records (invoices, ledgers).
    Covers every PII-bearing table keyed to the user or their account(s)."""
    account_ids_pre = await profile_svc.my_account_ids(db, user)
    if account_ids_pre and await db.scalar(
        select(func.count()).select_from(WithdrawalRequest).where(
            WithdrawalRequest.account_id.in_(account_ids_pre),
            WithdrawalRequest.status == "requested",
        )
    ):
        # Erasing the identity would leave a payout pending to someone we can no longer
        # identify (or let it be approved to an erased subject): settle it first.
        raise ErasureBlocked("A wallet withdrawal is pending; it must be paid or rejected first")
    orig_email = user.email  # capture before nulling (newsletter is keyed by email)
    orig_verified = user.email_verified_at is not None
    # base-36 keeps the unique tombstone within phone's VARCHAR(16) for any id size.
    user.phone = f"erz-{_b36(user.id)}"
    user.email = None
    user.email_verified_at = None
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
        # Home shops hold the delivery address copied at checkout / subscription (P1):
        # tombstone them and stop every standing order so nothing keeps generating
        # deliveries + debits against the erased person.
        await db.execute(
            update(Shop)
            .where(Shop.account_id.in_(account_ids), Shop.is_home.is_(True))
            .values(name="erased", address=None, pincode=None, geo=None, status="inactive")
        )
        await db.execute(
            update(Subscription)
            .where(
                Subscription.account_id.in_(account_ids),
                Subscription.status.in_(("active", "paused")),
            )
            .values(status="cancelled")
        )
        # Deliveries already generated/frozen for tomorrow would still be driven to the
        # tombstoned address and billed to the erased account: cancel them.
        await db.execute(
            update(DeliveryOrder)
            .where(
                DeliveryOrder.account_id.in_(account_ids),
                DeliveryOrder.status.in_(("generated", "frozen")),
            )
            .values(status="cancelled", failure_reason="account_erased")
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
    # Anything still waiting in the outbox must never go out (it'd send a blank message).
    await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.status.in_(("queued", "sending")))
        .values(status="skipped", skip_reason="user_erased")
    )
    # Scrub rendered PII from the notification outbox (to_addr = phone/token, body/payload).
    await db.execute(
        update(Notification).where(Notification.user_id == user.id).values(
            to_addr=None, body=None, payload=None
        )
    )
    # A delivery user's route assignments end with the account.
    await db.execute(
        update(DeliveryAssignment)
        .where(DeliveryAssignment.delivery_id == user.id)
        .values(active=False)
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
    # Auth sessions: REVOKE every refresh token (token_version alone only kills access
    # tokens) and scrub the request metadata (IP / user-agent / device name).
    await db.execute(
        update(AuthSession).where(AuthSession.user_id == user.id).values(
            ip=None, user_agent=None, device_name=None, revoked=True
        )
    )
    # Driver GPS ping history (if the subject was a delivery user).
    await db.execute(delete(DriverLocation).where(DriverLocation.driver_id == user.id))
    # Newsletter subscription (keyed by email, not user_id) — fully remove, but ONLY when
    # the subject proved they own that address: an unverified account email could be
    # someone else's (squatting), and erasing would delete the real owner's subscription.
    if orig_email and orig_verified:
        await db.execute(
            delete(NewsletterSubscriber).where(
                func.lower(NewsletterSubscriber.email) == orig_email.lower()
            )
        )

    for purpose in (
        "whatsapp_optin", "marketing_sms", "marketing_email", "marketing_push",
        "marketing_whatsapp", "data_processing",
    ):
        db.add(Consent(user_id=user.id, purpose=purpose, action="withdrawn", source="erasure"))

    await db.commit()
    return {"detail": "account erased", "user_id": user.id, "retained": "financial/tax records"}


async def owner_user_for_account(db: AsyncSession, account_id: int) -> User | None:
    account = await db.get(CustomerAccount, account_id)
    return await db.get(User, account.owner_user_id) if account else None
