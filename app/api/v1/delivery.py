import asyncio
import datetime as dt
import secrets

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import media
from app.core.db import get_db
from app.core.ratelimit_quota import upload_quota_ok
from app.core.redis import redis_client
from app.core.storage import put_private_object
from app.core.uploads import precheck_upload_size, read_validated_image, strip_metadata
from app.deps.auth import get_current_user
from app.deps.pagination import Page, pagination
from app.deps.scope import ALL, allowed_shop_ids
from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.driver import DriverLocation
from app.models.fulfillment import CashCollection
from app.models.user import User
from app.schemas.delivery import DeliverRequest, DeliveryOrderItemOut, DeliveryOrderOut
from app.services import delivery as delivery_service
from app.services.subscription import today_ist

router = APIRouter(prefix="/delivery", tags=["delivery"])
_PING_EVERY_S = 5


class UploadOut(BaseModel):
    url: str  # the reference to pass as pod.photo_url/signature_url (media:<key>)
    preview_url: str


@router.post("/uploads", operation_id="upload_pod_asset", response_model=UploadOut)
async def upload_pod_asset(
    request: Request, file: UploadFile = File(...), user: User = Depends(get_current_user)
):
    """Upload a POD photo/signature; returns a URL to pass as pod.photo_url/signature_url.
    Validated by magic bytes and stored with a server-chosen content-type."""
    if user.role not in ("delivery", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only delivery staff can upload POD assets")
    precheck_upload_size(request)
    if not await upload_quota_ok(user.id, per_day=1000):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Upload limit reached; try tomorrow")
    data, ext, ctype = await read_validated_image(file)
    # Doorstep photos are personal data: private bucket, EXIF (GPS) stripped.
    data = strip_metadata(data, ext)
    key = await asyncio.to_thread(
        put_private_object, f"pod/{user.id}/{secrets.token_hex(8)}.{ext}", data, ctype
    )
    ref = media.make_ref(key)
    return {"url": ref, "preview_url": media.resolve(ref)}


async def _attach_stop_details(db: AsyncSession, views: list[DeliveryOrderOut]) -> None:
    """Fill each stop's shop address/pincode/geo + the account owner's name/phone, so
    the delivery app knows where to go and who to hand to (batched; no N+1)."""
    if not views:
        return
    shop_ids = {v.shop_id for v in views}
    rows = (
        await db.execute(
            text(
                "SELECT s.id, s.name, s.address, s.pincode, s.account_id, "
                "ST_Y(s.geo::geometry) AS lat, ST_X(s.geo::geometry) AS lng "
                "FROM shops s WHERE s.id = ANY(:ids)"
            ),
            {"ids": list(shop_ids)},
        )
    ).mappings().all()
    shop = {r["id"]: r for r in rows}
    account_ids = {r["account_id"] for r in rows}
    contacts: dict[int, tuple[str | None, str | None]] = {}
    if account_ids:
        crows = (
            await db.execute(
                text(
                    "SELECT a.id, u.full_name, u.phone FROM customer_accounts a "
                    "JOIN users u ON u.id = a.owner_user_id WHERE a.id = ANY(:ids)"
                ),
                {"ids": list(account_ids)},
            )
        ).mappings().all()
        contacts = {r["id"]: (r["full_name"], r["phone"]) for r in crows}
    for v in views:
        s = shop.get(v.shop_id)
        if s is None:
            continue
        v.shop_name, v.address, v.pincode = s["name"], s["address"], s["pincode"]
        v.lat = float(s["lat"]) if s["lat"] is not None else None
        v.lng = float(s["lng"]) if s["lng"] is not None else None
        name, phone = contacts.get(s["account_id"], (None, None))
        v.contact_name, v.contact_phone = name, phone


async def _order_detail(db: AsyncSession, order: DeliveryOrder) -> DeliveryOrderOut:
    res = await db.execute(
        select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
    )
    out = DeliveryOrderOut.model_validate(order)
    out.items = [DeliveryOrderItemOut.model_validate(i) for i in res.scalars().all()]
    await _attach_stop_details(db, [out])
    return out


async def _order_details(db: AsyncSession, orders: list[DeliveryOrder]) -> list[DeliveryOrderOut]:
    """Build delivery-order views with a single batched item query (no N+1)."""
    if not orders:
        return []
    ids = [o.id for o in orders]
    res = await db.execute(
        select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id.in_(ids))
    )
    by_order: dict[int, list[DeliveryOrderItemOut]] = {}
    for item in res.scalars().all():
        by_order.setdefault(item.delivery_order_id, []).append(
            DeliveryOrderItemOut.model_validate(item)
        )
    views = []
    for o in orders:
        view = DeliveryOrderOut.model_validate(o)
        view.items = by_order.get(o.id, [])
        views.append(view)
    await _attach_stop_details(db, views)
    return views


@router.get("/orders", response_model=list[DeliveryOrderOut], operation_id="list_delivery_orders")
async def list_delivery_orders(
    date: dt.date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    """Delivery orders for a date, scoped to the caller's shops (delivery=assigned,
    subscriber=owned, admin=all)."""
    for_date = date or (today_ist() + dt.timedelta(days=1))
    ids = await allowed_shop_ids(user, db)

    query = select(DeliveryOrder).where(DeliveryOrder.delivery_date == for_date)
    if ids is not ALL:
        if not ids:
            return []
        query = query.where(DeliveryOrder.shop_id.in_(ids))
    orders = (
        await db.execute(
            query.order_by(DeliveryOrder.id).limit(page.limit).offset(page.offset)
        )
    ).scalars().all()
    return await _order_details(db, list(orders))


@router.post(
    "/orders/{order_id}/deliver", response_model=DeliveryOrderOut, operation_id="deliver_order"
)
async def deliver_order(
    order_id: int,
    data: DeliverRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a stop delivered: per-item quantities (partial OK), POD, returns, cash."""
    if user.role not in ("delivery", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only delivery staff can mark deliveries")

    res = await db.execute(
        select(DeliveryOrder).where(DeliveryOrder.id == order_id).with_for_update()
    )
    order = res.scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    ids = await allowed_shop_ids(user, db)
    if ids is not ALL and order.shop_id not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")

    await delivery_service.mark_delivered(db, order, user, data)
    return await _order_detail(db, order)


# ---------- Delivery-app operations (route, location, summary, failure) ----------
class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)


class OkOut(BaseModel):
    ok: bool


class RouteStopOut(BaseModel):
    sequence: int
    order: DeliveryOrderOut


class SummaryOut(BaseModel):
    date: dt.date
    delivered: int
    failed: int
    pending: int
    cash_collected: float


class FailIn(BaseModel):
    reason: str = Field(pattern="^(customer_unavailable|address_wrong|refused|other)$")


def _require_delivery(user: User) -> None:
    if user.role not in ("delivery", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Delivery staff only")


@router.post("/location", response_model=OkOut, operation_id="post_driver_location")
async def post_driver_location(
    data: LocationIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Driver GPS ping (append-only; latest row is the driver's current position)."""
    _require_delivery(user)
    # At most one stored ping per driver per few seconds: a misbehaving app (or script)
    # can't grow the table without bound. Extra pings are acknowledged and dropped.
    try:
        fresh = await redis_client.set(f"drvloc:{user.id}", 1, nx=True, ex=_PING_EVERY_S)
    except Exception:
        fresh = True
    if fresh:
        db.add(DriverLocation(driver_id=user.id, lat=data.lat, lng=data.lng))
        await db.commit()
    return {"ok": True}


@router.get("/orders/route", response_model=list[RouteStopOut], operation_id="delivery_route")
async def delivery_route(
    date: dt.date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's assigned stops for a date as an ordered route with sequence numbers.
    Ordered by shop_id (stable); geo nearest-neighbor from the driver's latest location
    is a future enhancement once shops carry geo."""
    for_date = date or (today_ist() + dt.timedelta(days=1))
    ids = await allowed_shop_ids(user, db)
    q = select(DeliveryOrder).where(DeliveryOrder.delivery_date == for_date)
    if ids is not ALL:
        if not ids:
            return []
        q = q.where(DeliveryOrder.shop_id.in_(ids))
    orders = (await db.execute(q.order_by(DeliveryOrder.shop_id).limit(1000))).scalars().all()
    details = await _order_details(db, list(orders))
    return [RouteStopOut(sequence=i + 1, order=d) for i, d in enumerate(details)]


@router.get("/summary", response_model=SummaryOut, operation_id="delivery_summary")
async def delivery_summary(
    date: dt.date | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-day counts (delivered/failed/pending) + cash collected, scoped to the caller."""
    _require_delivery(user)
    for_date = date or today_ist()
    ids = await allowed_shop_ids(user, db)
    # Scope filter reused by both aggregates — never load the day's orders into Python.
    scope = [DeliveryOrder.delivery_date == for_date]
    if ids is not ALL:
        if not ids:
            return SummaryOut(date=for_date, delivered=0, failed=0, pending=0, cash_collected=0)
        scope.append(DeliveryOrder.shop_id.in_(ids))

    status_rows = (
        await db.execute(
            select(DeliveryOrder.status, func.count())
            .where(*scope)
            .group_by(DeliveryOrder.status)
        )
    ).all()
    counts = {s: int(n) for s, n in status_rows}
    delivered = counts.get("delivered", 0) + counts.get("partially_delivered", 0)
    failed = counts.get("failed", 0)
    pending = counts.get("generated", 0) + counts.get("frozen", 0)

    # Cash collected over ALL the day's in-scope orders — a failed/partial stop that still
    # took cash must count toward the day total (#delivery-3), not just 'delivered' ones.
    cash = float(
        await db.scalar(
            select(func.coalesce(func.sum(CashCollection.amount_collected), 0))
            .select_from(CashCollection)
            .join(DeliveryOrder, DeliveryOrder.id == CashCollection.delivery_order_id)
            .where(*scope)
        )
        or 0
    )
    return SummaryOut(
        date=for_date, delivered=delivered, failed=failed, pending=pending, cash_collected=cash
    )


@router.post(
    "/orders/{order_id}/fail", response_model=DeliveryOrderOut, operation_id="fail_delivery_order"
)
async def fail_delivery_order(
    order_id: int,
    data: FailIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark an assigned stop failed with a reason (no billing/stock movement)."""
    _require_delivery(user)
    order = (
        await db.execute(
            select(DeliveryOrder).where(DeliveryOrder.id == order_id).with_for_update()
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    ids = await allowed_shop_ids(user, db)
    if ids is not ALL and order.shop_id not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if user.role != "admin" and order.delivery_date > today_ist():
        raise HTTPException(status.HTTP_409_CONFLICT, "This stop is scheduled for a later date")
    if order.status not in ("generated", "frozen"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Order is {order.status}; cannot mark failed"
        )
    order.status = "failed"
    order.failure_reason = data.reason
    await db.commit()
    return await _order_detail(db, order)
