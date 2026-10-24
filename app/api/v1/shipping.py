"""Courier shipping (Shiprocket): serviceability, create/track/cancel, webhook.

Admin creates shipments for one-time orders; customers track their own (IDOR-safe);
Shiprocket posts tracking updates to the webhook. Dev-stubbed until creds are set.
"""
import datetime as dt
import hmac

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, pagination
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
    pincode: str = Query(pattern=r"^\d{6}$"),
    weight_kg: float = Query(default=0.5, gt=0, le=100),
    cod: bool = False,
    _user: User = Depends(get_current_user),
):
    return await shipping.check_serviceability(pincode, weight_kg, cod)


# ---------- Admin: create / cancel ----------
@router.post("/admin/shipments", response_model=ShipmentOut, operation_id="create_shipment",
             dependencies=[Depends(require_role("admin"))])
async def create_shipment(data: ShipmentCreateIn, db: AsyncSession = Depends(get_db)):
    # The service manages its own locking/commits (draft -> courier call -> finalize) so
    # no DB row lock is held across the courier HTTP call (S1).
    dest = {
        "name": data.name, "phone": data.phone, "address": data.address,
        "city": data.city, "state": data.state, "pincode": data.pincode,
    }
    ship = await ship_svc.create_shipment(db, data.order_id, dest, data.weight_kg, data.cod)
    return await _shipment_out(db, ship)


@router.post("/admin/shipments/{shipment_id}/cancel", response_model=ShipmentOut,
             operation_id="cancel_shipment", dependencies=[Depends(require_role("admin"))])
async def cancel_shipment(shipment_id: int, db: AsyncSession = Depends(get_db)):
    # The service manages its own locking/commits (claim -> courier call -> finalize) so
    # no DB row lock is held across the courier HTTP call (#74).
    ship = await ship_svc.cancel_shipment(db, shipment_id)
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
        q = q.where(Shipment.status != "draft")  # not a parcel yet (create in flight/failed)
    if order_id is not None:
        q = q.where(Shipment.order_id == order_id)
    ships = (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()
    # ONE batched events query for the page (was one query per shipment — N+1).
    by_ship: dict[int, list[TrackingEventOut]] = {}
    if ships:
        for e in (
            await db.execute(
                select(ShipmentTrackingEvent)
                .where(ShipmentTrackingEvent.shipment_id.in_([s_.id for s_ in ships]))
                .order_by(ShipmentTrackingEvent.shipment_id, ShipmentTrackingEvent.id)
            )
        ).scalars():
            by_ship.setdefault(e.shipment_id, []).append(TrackingEventOut.model_validate(e))
    outs = []
    for s_ in ships:
        out = ShipmentOut.model_validate(s_)
        out.events = by_ship.get(s_.id, [])
        outs.append(out)
    return outs


@router.get("/shipments/{shipment_id}", response_model=ShipmentOut, operation_id="get_shipment")
async def get_shipment(
    shipment_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    ship = await db.get(Shipment, shipment_id)
    # 404 (not 403) on cross-account access so IDs aren't enumerable.
    if ship is None or (
        user.role != "admin"
        and (ship.status == "draft" or ship.account_id not in await _my_account_ids(db, user))
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shipment not found")
    return await _shipment_out(db, ship)


# ---------- Shiprocket tracking webhook ----------
@router.post("/shipping/webhook", response_model=WebhookAck, operation_id="shipping_webhook")
async def shipping_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    # Shiprocket echoes a configured token in the x-api-key header. Compare in constant
    # time so the token can't be recovered byte-by-byte via timing.
    if settings.SHIPROCKET_WEBHOOK_TOKEN:
        if not hmac.compare_digest(
            request.headers.get("x-api-key") or "", settings.SHIPROCKET_WEBHOOK_TOKEN
        ):
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
    raw_status = str(raw_status)

    ship = (
        await db.execute(select(Shipment).where(Shipment.awb == awb).with_for_update())
    ).scalar_one_or_none()
    if ship is None:
        return {"status": "unknown_awb"}

    mapped = ship_svc.map_status(raw_status)
    if mapped and ship.status == mapped:  # idempotent: same status already applied
        return {"status": "duplicate"}

    # Dedupe replays of the SAME event (incl. unmapped statuses, which don't change
    # ship.status) so a re-POST doesn't append a duplicate tracking row.
    # External input: coerce to text (a number/object here used to TypeError on [:120]).
    loc = payload.get("location")
    act = payload.get("activity")
    loc = str(loc) if loc is not None and not isinstance(loc, str) else loc
    act = str(act) if act is not None and not isinstance(act, str) else act
    event_status = mapped or str(raw_status)[:32]
    last = await db.scalar(
        select(ShipmentTrackingEvent)
        .where(ShipmentTrackingEvent.shipment_id == ship.id)
        .order_by(ShipmentTrackingEvent.id.desc())
        .limit(1)
    )
    if (
        last is not None
        and last.status == event_status
        and last.location == (loc[:120] if loc else None)
        and last.note == (act[:255] if act else None)
    ):
        return {"status": "duplicate"}

    # Parse the courier's own timestamp if present (best-effort) for event ordering.
    event_time = None
    for field in ("event_time", "timestamp", "date"):
        val = payload.get(field)
        if val:
            try:
                event_time = dt.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except ValueError:
                event_time = None
            break

    await ship_svc.apply_tracking(
        db, ship, raw_status,
        location=loc, note=act, raw=payload, event_time=event_time,
    )
    await db.commit()
    return {"status": "ok"}
