import datetime as dt
import secrets

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.storage import put_object
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

_MAX_UPLOAD = 5 * 1024 * 1024  # 5 MB
_ALLOWED_UPLOAD = {"image/jpeg", "image/png", "image/webp"}


class UploadOut(BaseModel):
    url: str


@router.post("/uploads", operation_id="upload_pod_asset", response_model=UploadOut)
async def upload_pod_asset(
    file: UploadFile = File(...), user: User = Depends(get_current_user)
):
    """Upload a POD photo/signature; returns a URL to pass as pod.photo_url/signature_url."""
    if user.role not in ("delivery", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only delivery staff can upload POD assets")
    if file.content_type not in _ALLOWED_UPLOAD:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Only JPEG/PNG/WEBP allowed")
    data = await file.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large (max 5 MB)")
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[file.content_type]
    key = f"pod/{user.id}/{secrets.token_hex(8)}.{ext}"
    url = put_object(key, data, file.content_type)
    return {"url": url}


async def _order_detail(db: AsyncSession, order: DeliveryOrder) -> DeliveryOrderOut:
    res = await db.execute(
        select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
    )
    out = DeliveryOrderOut.model_validate(order)
    out.items = [DeliveryOrderItemOut.model_validate(i) for i in res.scalars().all()]
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
    q = select(DeliveryOrder).where(DeliveryOrder.delivery_date == for_date)
    if ids is not ALL:
        if not ids:
            return SummaryOut(date=for_date, delivered=0, failed=0, pending=0, cash_collected=0)
        q = q.where(DeliveryOrder.shop_id.in_(ids))
    orders = (await db.execute(q)).scalars().all()
    delivered = [o for o in orders if o.status in ("delivered", "partially_delivered")]
    failed = sum(1 for o in orders if o.status == "failed")
    pending = sum(1 for o in orders if o.status in ("generated", "frozen"))
    cash = 0.0
    if delivered:
        cash = float(
            await db.scalar(
                select(func.coalesce(func.sum(CashCollection.amount_collected), 0)).where(
                    CashCollection.delivery_order_id.in_([o.id for o in delivered])
                )
            )
            or 0
        )
    return SummaryOut(
        date=for_date, delivered=len(delivered), failed=failed, pending=pending, cash_collected=cash
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
    if order.status not in ("generated", "frozen"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Order is {order.status}; cannot mark failed"
        )
    order.status = "failed"
    order.failure_reason = data.reason
    await db.commit()
    return await _order_detail(db, order)
