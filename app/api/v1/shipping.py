"""Courier shipping (Shiprocket): serviceability, create/track/cancel, webhook.

Admin creates shipments for one-time orders; customers track their own (IDOR-safe);
Shiprocket posts tracking updates to the webhook. Dev-stubbed until creds are set.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, pagination
from app.models.order import Order
from app.models.shipment import Shipment, ShipmentTrackingEvent
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import shipping
from app.services.shipping import service as ship_svc

router = APIRouter(tags=["shipping"])


# ---------- Schemas ----------
class ServiceabilityOut(BaseModel):
    serviceable: bool
    etd_days: int | None = None
    courier: str | None = None
    rate: float | None = None


class ShipmentCreateIn(BaseModel):
    order_id: int
    pincode: str = Field(min_length=4, max_length=10)
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=16)
    address: str | None = Field(default=None, max_length=512)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    weight_kg: float = Field(default=0.5, gt=0, le=100, allow_inf_nan=False)
    cod: bool = False


class TrackingEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: str
    location: str | None = None
    note: str | None = None


class ShipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shipment_no: str
    order_id: int
    status: str
    courier: str | None = None
    awb: str | None = None
    tracking_url: str | None = None
    to_pincode: str
    cod_amount: float
    events: list[TrackingEventOut] = []


class WebhookAck(BaseModel):
    status: str


async def _my_account_ids(db: AsyncSession, user: User) -> set[int]:
    return set(
        (
            await db.execute(
                select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id)
            )
        ).scalars().all()
    )


async def _shipment_out(db: AsyncSession, ship: Shipment) -> ShipmentOut:
    events = (
        await db.execute(
            select(ShipmentTrackingEvent)
            .where(ShipmentTrackingEvent.shipment_id == ship.id)
            .order_by(ShipmentTrackingEvent.id)
        )
    ).scalars().all()
    out = ShipmentOut.model_validate(ship)
    out.events = [TrackingEventOut.model_validate(e) for e in events]
    return out


# ---------- Serviceability (customer) ----------
@router.get("/shipping/serviceability", response_model=ServiceabilityOut, operation_id="check_serviceability")
async def serviceability(
    pincode: str,
    weight_kg: float = 0.5,
    cod: bool = False,
    _user: User = Depends(get_current_user),
):
    return await shipping.check_serviceability(pincode, weight_kg, cod)


# ---------- Admin: create / cancel ----------
@router.post("/admin/shipments", response_model=ShipmentOut, operation_id="create_shipment",
             dependencies=[Depends(require_role("admin"))])
async def create_shipment(data: ShipmentCreateIn, db: AsyncSession = Depends(get_db)):
    # Lock the order so two concurrent creates can't both make a shipment for it.
    order = (
        await db.execute(select(Order).where(Order.id == data.order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    dest = {
        "name": data.name, "phone": data.phone, "address": data.address,
        "city": data.city, "state": data.state, "pincode": data.pincode,
    }
    ship = await ship_svc.create_shipment(db, order, dest, data.weight_kg, data.cod)
    await db.commit()
    await db.refresh(ship)
    return await _shipment_out(db, ship)


@router.post("/admin/shipments/{shipment_id}/cancel", response_model=ShipmentOut,
             operation_id="cancel_shipment", dependencies=[Depends(require_role("admin"))])
async def cancel_shipment(shipment_id: int, db: AsyncSession = Depends(get_db)):
    ship = (
        await db.execute(select(Shipment).where(Shipment.id == shipment_id).with_for_update())
    ).scalar_one_or_none()
    if ship is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shipment not found")
    await ship_svc.cancel_shipment(db, ship)
    await db.commit()
    await db.refresh(ship)
    return await _shipment_out(db, ship)


# ---------- Customer: list / track (IDOR-safe) ----------
@router.get("/shipments", response_model=list[ShipmentOut], operation_id="list_shipments")
async def list_shipments(
    order_id: int | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Shipment).order_by(Shipment.id.desc())
    if user.role != "admin":  # customers see only their own account's shipments
        q = q.where(Shipment.account_id.in_(await _my_account_ids(db, user)))
    if order_id is not None:
        q = q.where(Shipment.order_id == order_id)
    ships = (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()
    return [await _shipment_out(db, s) for s in ships]


@router.get("/shipments/{shipment_id}", response_model=ShipmentOut, operation_id="get_shipment")
async def get_shipment(
    shipment_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    ship = await db.get(Shipment, shipment_id)
    # 404 (not 403) on cross-account access so IDs aren't enumerable.
    if ship is None or (
        user.role != "admin" and ship.account_id not in await _my_account_ids(db, user)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shipment not found")
    return await _shipment_out(db, ship)


# ---------- Shiprocket tracking webhook ----------
@router.post("/shipping/webhook", response_model=WebhookAck, operation_id="shipping_webhook")
async def shipping_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    # Shiprocket echoes a configured token in the x-api-key header.
    if settings.SHIPROCKET_WEBHOOK_TOKEN:
        if request.headers.get("x-api-key") != settings.SHIPROCKET_WEBHOOK_TOKEN:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook token")
    elif settings.ENV == "prod":
        # Fail closed in prod (defense in depth; the boot guard also requires the token).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Webhook token not configured")
    import json

    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON") from None

    awb = str(payload.get("awb") or "").strip()
    raw_status = payload.get("current_status") or payload.get("shipment_status")
    if not awb or not raw_status:
        return {"status": "ignored"}

    ship = (
        await db.execute(select(Shipment).where(Shipment.awb == awb).with_for_update())
    ).scalar_one_or_none()
    if ship is None:
        return {"status": "unknown_awb"}

    mapped = ship_svc.map_status(raw_status)
    if mapped and ship.status == mapped:  # idempotent: same status already applied
        return {"status": "duplicate"}

    await ship_svc.apply_tracking(
        db, ship, raw_status,
        location=payload.get("location"), note=payload.get("activity"), raw=payload,
    )
    await db.commit()
    return {"status": "ok"}
