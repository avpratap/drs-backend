"""Shipment lifecycle: create (→ courier order + AWB), apply tracking, cancel."""
import datetime as dt
from uuid import uuid4

from fastapi import HTTPException
from fastapi import status as http
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shipment import Shipment, ShipmentTrackingEvent
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import notifications as notify
from app.services.shipping import client

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


async def create_shipment(db: AsyncSession, order: Order, dest: dict, weight_kg: float, cod: bool) -> Shipment:
    if order.payment_status not in ("paid", "pending"):  # refunded/cancelled can't ship
        raise HTTPException(http.HTTP_409_CONFLICT, f"Order is {order.payment_status}, cannot ship")
    if order.status in ("cancelled", "refunded"):
        raise HTTPException(http.HTTP_409_CONFLICT, f"Order is {order.status}, cannot ship")
    # A prepaid (non-COD) order must be PAID before goods leave — only COD shipments
    # (money collected on delivery) may ship while payment is still pending.
    if not cod and order.payment_status != "paid":
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            "Prepaid order must be paid before a non-COD shipment",
        )
    # One live shipment per order (a cancelled one may be superseded).
    existing = await db.scalar(
        select(Shipment.id).where(Shipment.order_id == order.id, Shipment.status != "cancelled")
    )
    if existing is not None:
        raise HTTPException(http.HTTP_409_CONFLICT, "A shipment already exists for this order")
    ship = Shipment(
        shipment_no=f"PENDING-{uuid4().hex[:20]}",
        order_id=order.id,
        account_id=order.account_id,
        status="draft",
        weight_kg=weight_kg,
        cod_amount=float(order.total) if cod else 0,
        to_name=dest.get("name"),
        to_phone=dest.get("phone"),
        to_address=dest.get("address"),
        to_city=dest.get("city"),
        to_state=dest.get("state"),
        to_pincode=dest["pincode"],
    )
    db.add(ship)
    await db.flush()
    ship.shipment_no = f"SHP{ship.id:06d}"

    items = await _order_items_payload(db, order)
    sr_order = {
        "order_id": ship.shipment_no,
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
        "order_items": items,
        "payment_method": "COD" if cod else "Prepaid",
        "sub_total": float(order.total),
        "length": 10, "breadth": 10, "height": 10, "weight": weight_kg,
    }
    result = await client.create_shipment({"shipment_id": ship.id, "order": sr_order})
    ship.gateway_order_id = result.get("gateway_order_id")
    ship.gateway_shipment_id = result.get("gateway_shipment_id")
    ship.awb = result.get("awb")
    ship.courier = result.get("courier")
    ship.tracking_url = result.get("tracking_url")
    ship.label_url = result.get("label_url")
    ship.status = "created"
    db.add(ShipmentTrackingEvent(shipment_id=ship.id, status="created", note="Shipment created + AWB assigned"))
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
            await _notify_delivered(db, ship)
    return ship


async def _notify_delivered(db: AsyncSession, ship: Shipment) -> None:
    account = await db.get(CustomerAccount, ship.account_id)
    owner = await db.get(User, account.owner_user_id) if account else None
    if owner is not None:
        await notify.emit_event(
            db, "shipment.delivered", owner, {"shipment_no": ship.shipment_no},
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
    ok = await client.cancel_shipment(gsid)

    # Phase C — re-lock and finalize, respecting any terminal state a webhook set meanwhile.
    ship = (
        await db.execute(
            select(Shipment).where(Shipment.id == shipment_id).with_for_update()
        )
    ).scalar_one()
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
