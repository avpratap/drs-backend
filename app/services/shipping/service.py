"""Shipment lifecycle: create (→ courier order + AWB), apply tracking, cancel."""
import datetime as dt

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
    ship = Shipment(
        shipment_no="PENDING",
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
    db: AsyncSession, ship: Shipment, raw_status: str, *, location=None, note=None, raw=None
) -> Shipment:
    mapped = map_status(raw_status)
    db.add(ShipmentTrackingEvent(
        shipment_id=ship.id, status=mapped or raw_status[:32], location=location, note=note, raw=raw,
    ))
    # Don't regress out of a terminal state on a late/duplicate event.
    if mapped and ship.status not in _TERMINAL:
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


async def cancel_shipment(db: AsyncSession, ship: Shipment) -> Shipment:
    if ship.status in _TERMINAL:
        raise HTTPException(http.HTTP_409_CONFLICT, f"Shipment is {ship.status}, cannot cancel")
    ok = await client.cancel_shipment(ship.gateway_shipment_id or "")
    if not ok:
        raise HTTPException(http.HTTP_502_BAD_GATEWAY, "Courier cancel failed; try again")
    ship.status = "cancelled"
    db.add(ShipmentTrackingEvent(shipment_id=ship.id, status="cancelled", note="Cancelled by admin"))
    return ship
