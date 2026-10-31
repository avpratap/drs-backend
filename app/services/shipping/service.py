"""Shipment lifecycle: create (→ courier order + AWB), apply tracking, cancel."""
import datetime as dt
import logging
from uuid import uuid4

from fastapi import HTTPException
from fastapi import status as http
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shipment import Shipment, ShipmentTrackingEvent
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import notifications as notify
from app.services.shipping import client

log = logging.getLogger("drs.shipping")

# Map courier/webhook status text -> our canonical status.
_STATUS_MAP = {
    "created": "created",
    "pickup scheduled": "created",
    "pickup generated": "created",
    "in transit": "in_transit",
    "shipped": "in_transit",
    "out for delivery": "out_for_delivery",
    "delivered": "delivered",
    "rto": "rto",
    "rto initiated": "rto",
    "rto delivered": "rto",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "failed": "failed",
}

# How long a draft's in-flight courier create is exclusively claimed. Comfortably above
# the client's worst case (2 calls x (login + request + one 401 retry) at 15-20s each).
_CREATE_LEASE = dt.timedelta(minutes=5)

_TERMINAL = {"delivered", "rto", "cancelled", "failed"}

# Lifecycle rank so an out-of-order/late webhook can't regress a shipment (e.g. a stale
# "in transit" arriving after "out for delivery").
_RANK = {
    "draft": 0, "created": 1, "in_transit": 2, "out_for_delivery": 3,
    "delivered": 4, "rto": 4, "cancelled": 4, "failed": 4,
}


def map_status(raw: str | None) -> str | None:
    if not raw:
        return None
    return _STATUS_MAP.get(raw.strip().lower())


async def _order_items_payload(db: AsyncSession, order: Order) -> list[dict]:
    rows = (
        await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    ).scalars().all()
    out = []
    for it in rows:
        prod = await db.get(Product, it.product_id)
        out.append({
            "name": prod.name if prod else f"Product {it.product_id}",
            "sku": prod.sku if prod else str(it.product_id),
            "units": float(it.qty),
            "selling_price": float(it.unit_price),
            "hsn": (prod.hsn_code if prod else None),
        })
    return out


def _unshippable_reason(order: Order, cod: bool) -> str | None:
    if order.payment_status not in ("paid", "pending"):  # refunded/cancelled can't ship
        return f"Order is {order.payment_status}, cannot ship"
    if order.status in ("cancelled", "refunded"):
        return f"Order is {order.status}, cannot ship"
    # A prepaid (non-COD) order must be PAID before goods leave — only COD shipments
    # (money collected on delivery) may ship while payment is still pending (#79).
    if not cod and order.payment_status != "paid":
        return "Prepaid order must be paid before a non-COD shipment"
    return None


async def _courier_order(
    db: AsyncSession, order: Order, shipment_no: str, dest: dict, weight_kg: float, cod: bool
) -> dict:
    return {
        "order_id": shipment_no,  # stable per draft, so a resumed create reuses the same key
        "order_date": dt.date.today().isoformat(),
        "pickup_location": settings.SHIPROCKET_PICKUP_LOCATION,
        "channel_id": settings.SHIPROCKET_CHANNEL_ID or None,
        "billing_customer_name": dest.get("name") or "Customer",
        "billing_last_name": "",
        "billing_address": dest.get("address") or "",
        "billing_city": dest.get("city") or "",
        "billing_pincode": dest["pincode"],
        "billing_state": dest.get("state") or "",
        "billing_country": "India",
        "billing_phone": dest.get("phone") or "",
        "shipping_is_billing": True,
        "order_items": await _order_items_payload(db, order),
        "payment_method": "COD" if cod else "Prepaid",
        "sub_total": float(order.total),
        "length": 10, "breadth": 10, "height": 10, "weight": weight_kg,
    }


async def _relock(db: AsyncSession, stmt):
    # populate_existing: the session keeps objects across commits (expire_on_commit=False),
    # so without it a re-lock would hand back the STALE pre-call state, not the row's.
    return (
        await db.execute(stmt.with_for_update().execution_options(populate_existing=True))
    ).scalar_one()


async def _lock(db: AsyncSession, order_id: int, shipment_id: int) -> tuple[Order, Shipment]:
    # Always order -> shipment (as in phase A) so concurrent creates can't deadlock.
    order = await _relock(db, select(Order).where(Order.id == order_id))
    ship = await _relock(db, select(Shipment).where(Shipment.id == shipment_id))
    return order, ship


async def _cancel_orphan(gateway_shipment_id: str | None) -> None:
    """Best-effort cancel of a courier order we created but can't keep (no lock held)."""
    if gateway_shipment_id and not await client.cancel_shipment(gateway_shipment_id):
        log.error("orphan courier shipment %s could not be cancelled", gateway_shipment_id)


async def _reclaim(
    db: AsyncSession, order_id: int, ship_id: int, lease: dt.datetime, cod: bool,
    orphan: str | None = None,
) -> tuple[Order, Shipment]:
    """Re-lock after a courier step and check we may still proceed (lock held on return).

    `orphan` is a courier shipment id we just created but have NOT persisted: on any
    bail-out it's ours to cancel (nobody else knows about it).
    """
    order, ship = await _lock(db, order_id, ship_id)
    if ship.status != "draft" or ship.create_lease_until != lease:
        # Superseded: the draft was cancelled (cancel_shipment already cancelled any
        # PERSISTED courier id), or our lease expired and another create took it over.
        await db.commit()
        await _cancel_orphan(orphan)
        if ship.status == "cancelled":
            raise HTTPException(http.HTTP_409_CONFLICT, "Shipment was cancelled during creation")
        if ship.status == "draft":
            raise HTTPException(
                http.HTTP_409_CONFLICT, "Shipment creation already in progress; retry shortly"
            )
        raise HTTPException(http.HTTP_409_CONFLICT, "A shipment already exists for this order")
    reason = _unshippable_reason(order, cod)
    if reason:
        # The order was cancelled/refunded during the courier call — don't ship it.
        gsid = orphan or ship.gateway_shipment_id
        ship.status = "cancelled"
        ship.create_lease_until = None
        db.add(ShipmentTrackingEvent(shipment_id=ship.id, status="cancelled", note=reason[:255]))
        await db.commit()
        await _cancel_orphan(gsid)
        raise HTTPException(http.HTTP_409_CONFLICT, reason)
    return order, ship


async def _abort(db: AsyncSession, order_id: int, ship_id: int, lease: dt.datetime) -> None:
    """A courier step failed: release OUR claim (only if still ours) so a retry resumes."""
    _, ship = await _lock(db, order_id, ship_id)
    if ship.status == "draft" and ship.create_lease_until == lease:
        ship.create_lease_until = None
    await db.commit()


async def create_shipment(
    db: AsyncSession, order_id: int, dest: dict, weight_kg: float, cod: bool
) -> Shipment:
    """Create a shipment WITHOUT holding a DB row lock across any courier HTTP call (S1).

    Mirrors cancel_shipment's phases. (A) Under a short order lock: validate (incl. the
    #79 paid/COD gate), persist a 'draft' with its final SHP number and an in-flight
    lease, and commit. (B) Courier calls with no lock/transaction held, each followed by
    (C) a re-lock that persists its result: create the courier order -> persist its ids;
    assign the AWB -> finalize to 'created'. A re-lock that finds the draft cancelled, the
    claim lost or the order no longer shippable bails out (cancelling any courier order
    only we know about).

    Resumable + idempotent: the draft is the order's one live shipment, so a retry after a
    courier failure or a crash resumes the SAME draft (same SHP number = the courier order
    key) — and, once the courier ids are persisted, skips straight to AWB assignment
    instead of creating a second courier order. While a draft's lease is live another
    create gets 409 'in progress', so concurrent creates can't both hit the courier; a
    request only ever clears/uses the lease it set itself.
    """
    # Phase A — validate + persist the draft under a short lock, then commit.
    order = (
        await db.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Order not found")
    reason = _unshippable_reason(order, cod)
    if reason:
        raise HTTPException(http.HTTP_409_CONFLICT, reason)
    # One live shipment per order (a cancelled one may be superseded).
    ship = (
        await db.execute(
            select(Shipment)
            .where(Shipment.order_id == order.id, Shipment.status != "cancelled")
            .with_for_update()
        )
    ).scalars().first()
    now = dt.datetime.now(dt.UTC)
    if ship is not None:
        if ship.status != "draft":
            raise HTTPException(http.HTTP_409_CONFLICT, "A shipment already exists for this order")
        if ship.create_lease_until is not None and ship.create_lease_until > now:
            raise HTTPException(
                http.HTTP_409_CONFLICT, "Shipment creation already in progress; retry shortly"
            )
        # else: resume the leftover draft (failed courier call or a crash mid-create).
    else:
        ship = Shipment(
            shipment_no=f"PENDING-{uuid4().hex[:20]}",
            order_id=order.id,
            account_id=order.account_id,
            status="draft",
            to_pincode=dest["pincode"],
        )
        db.add(ship)
        await db.flush()
        ship.shipment_no = f"SHP{ship.id:06d}"
    courier_order = None
    if ship.gateway_shipment_id is not None:
        # The courier order already exists with its original payment method: gate on THAT
        # (a retry flipping cod=True must not slip an unpaid prepaid parcel past #79).
        cod = float(ship.cod_amount) > 0
        reason = _unshippable_reason(order, cod)
        if reason:
            raise HTTPException(http.HTTP_409_CONFLICT, reason)
    else:
        # No courier order yet: (re)snapshot this request's parcel details. Once the
        # courier order exists its details are fixed, so a resume keeps the original.
        ship.weight_kg = weight_kg
        ship.cod_amount = float(order.total) if cod else 0
        ship.to_name = dest.get("name")
        ship.to_phone = dest.get("phone")
        ship.to_address = dest.get("address")
        ship.to_city = dest.get("city")
        ship.to_state = dest.get("state")
        ship.to_pincode = dest["pincode"]
        courier_order = await _courier_order(db, order, ship.shipment_no, dest, weight_kg, cod)
    lease = ship.create_lease_until = now + _CREATE_LEASE  # our claim token
    ship_id = ship.id
    await db.commit()  # release the locks before the network call

    if courier_order is not None:
        # Phase B1 — create the courier order with NO DB lock/transaction held open.
        try:
            ids = await client.create_order(ship_id, courier_order)
        except Exception as exc:
            log.warning("courier order create failed for shipment %s: %s", ship_id, exc)
            await _abort(db, order_id, ship_id, lease)
            raise HTTPException(http.HTTP_502_BAD_GATEWAY, "Courier create failed; try again") from None
        # Phase C1 — persist the courier ids at once, so a later failure resumes at B2.
        _, ship = await _reclaim(db, order_id, ship_id, lease, cod, orphan=ids["gateway_shipment_id"])
        ship.gateway_order_id = ids.get("gateway_order_id")
        ship.gateway_shipment_id = ids["gateway_shipment_id"]
        gsid = ship.gateway_shipment_id
        await db.commit()
    else:
        gsid = ship.gateway_shipment_id

    # Phase B2 — assign the AWB, again with no lock held.
    try:
        awb = await client.assign_awb(ship_id, gsid)
    except Exception as exc:
        log.warning("courier AWB assignment failed for shipment %s: %s", ship_id, exc)
        await _abort(db, order_id, ship_id, lease)
        raise HTTPException(http.HTTP_502_BAD_GATEWAY, "Courier create failed; try again") from None

    # Phase C2 — re-lock and finalize.
    _, ship = await _reclaim(db, order_id, ship_id, lease, cod)
    ship.awb = awb.get("awb")
    ship.courier = awb.get("courier")
    ship.tracking_url = awb.get("tracking_url")
    ship.label_url = awb.get("label_url")
    ship.status = "created"
    ship.create_lease_until = None
    db.add(ShipmentTrackingEvent(shipment_id=ship.id, status="created", note="Shipment created + AWB assigned"))
    await db.commit()
    return ship


async def apply_tracking(
    db: AsyncSession, ship: Shipment, raw_status: str, *,
    location=None, note=None, raw=None, event_time=None,
) -> Shipment:
    mapped = map_status(raw_status)
    # Truncate external courier strings to their column widths (location String(120),
    # note String(255)) so a long "activity" text is stored, not a DataError 500.
    db.add(ShipmentTrackingEvent(
        shipment_id=ship.id,
        status=mapped or raw_status[:32],
        location=(location[:120] if location else None),
        note=(note[:255] if note else None),
        raw=raw,
        event_time=event_time,  # courier's own timestamp (for ordering), not just created_at
    ))
    # Advance only forward: never regress out of a terminal state, and never to a
    # lower-rank status on a late/out-of-order webhook.
    if mapped and ship.status not in _TERMINAL and _RANK.get(mapped, 0) >= _RANK.get(ship.status, 0):
        was_delivered = ship.status == "delivered"
        ship.status = mapped
        if mapped == "delivered" and not was_delivered:
            if float(ship.cod_amount or 0) > 0:
                # Cash on delivery: the courier collected the money at the door, so the
                # order is now paid (COD orders otherwise stayed 'pending' forever: no
                # invoice, no verified review, and their coupon blocked re-use). Atomic
                # guarded UPDATE; drafts never receive webhooks, so no lock-order clash.
                res = await db.execute(
                    update(Order)
                    .where(Order.id == ship.order_id, Order.payment_status == "pending",
                           Order.status != "cancelled")
                    .values(payment_status="paid")
                )
                if res.rowcount:
                    from app.services import loyalty

                    await loyalty.reward_referral_on_purchase(db, ship.account_id)
            await _notify_delivered(db, ship)
    return ship


async def _notify_delivered(db: AsyncSession, ship: Shipment) -> None:
    account = await db.get(CustomerAccount, ship.account_id)
    owner = await db.get(User, account.owner_user_id) if account else None
    if owner is not None:
        await notify.emit_event(
            db, "shipment.delivered", owner,
            {"shipment_no": ship.shipment_no, "shipment_id": ship.id, "order_id": ship.order_id},
            account_id=ship.account_id, dedupe_key=f"shipment.delivered:{ship.id}",
        )
        # Post-delivery review prompt (marketing-gated by the notification router, so
        # it's quiet for users without consent). One per order.
        await notify.emit_event(
            db, "review.request", owner, {"order_id": ship.order_id},
            account_id=ship.account_id, dedupe_key=f"review.request:{ship.order_id}",
        )


async def cancel_shipment(db: AsyncSession, shipment_id: int) -> Shipment:
    """Cancel a shipment WITHOUT holding a DB row lock across the courier HTTP call (#74).

    No persisted intermediate status (an earlier 'cancelling' claim could be clobbered by
    a concurrent tracking webhook and could get permanently stuck on a crash). Instead:
    (A) validate under a short lock, then release it WITHOUT changing status; (B) call the
    courier with no lock held; (C) re-lock and finalize — but only if a webhook hasn't
    meanwhile moved the shipment to a terminal state (then that real status wins). A crash
    anywhere leaves the original status, so the operation is naturally retryable. Two
    concurrent cancels at worst duplicate the (idempotent) courier call; the DB is
    serialized to a single 'cancelled'.
    """
    # Phase A — validate under a short lock; do NOT persist an intermediate state.
    ship = (
        await db.execute(
            select(Shipment).where(Shipment.id == shipment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if ship is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, "Shipment not found")
    if ship.status in _TERMINAL:
        raise HTTPException(http.HTTP_409_CONFLICT, f"Shipment is {ship.status}, cannot cancel")
    gsid = ship.gateway_shipment_id or ""
    await db.commit()  # release the lock before the network call (no state change)

    # Phase B — courier call with NO DB lock/transaction held open.
    # A draft with no courier shipment yet has nothing to cancel at the courier.
    ok = await client.cancel_shipment(gsid) if gsid else True

    # Phase C — re-lock and finalize, respecting any terminal state a webhook set meanwhile.
    ship = await _relock(db, select(Shipment).where(Shipment.id == shipment_id))
    if (ship.gateway_shipment_id or "") != gsid:
        # An in-flight create persisted a courier order during our call, so what we
        # cancelled (nothing / an older id) isn't what now exists: change nothing.
        await db.commit()
        raise HTTPException(
            http.HTTP_409_CONFLICT, "Shipment changed during cancellation; try again"
        )
    if ship.status in _TERMINAL:
        # A tracking webhook reached a terminal state (e.g. delivered) during the courier
        # call — that real outcome wins; don't clobber it.
        await db.commit()
        if ship.status == "cancelled":
            return ship
        raise HTTPException(
            http.HTTP_409_CONFLICT, f"Shipment is now {ship.status}; cancellation not applied"
        )
    if not ok:
        await db.commit()  # nothing changed — naturally retryable
        raise HTTPException(http.HTTP_502_BAD_GATEWAY, "Courier cancel failed; try again")
    ship.status = "cancelled"
    db.add(ShipmentTrackingEvent(shipment_id=ship.id, status="cancelled", note="Cancelled by admin"))
    await db.commit()
    return ship
