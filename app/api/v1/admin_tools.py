"""Admin depth: audit log, CSV exports, feature flags, outbound webhooks, product search."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clientip import client_ip
from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, paged, pagination
from app.models.admin_tools import (
    AdminAuditLog,
    FeatureFlag,
    OutboundWebhook,
    OutboundWebhookDelivery,
)
from app.models.product import Product
from app.schemas.common import reject_nulls
from app.schemas.product import ProductOut
from app.services import admin_tools as svc

router = APIRouter(prefix="/admin", tags=["admin-tools"], dependencies=[Depends(require_role("admin"))])

_EXPORT_KINDS = ("orders", "payments", "users", "invoices")


# ---------- Schemas ----------
class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    admin_user_id: int
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    ip: str | None = None
    detail: dict | None = None
    created_at: dt.datetime | None = None


class FeatureFlagIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    enabled: bool = False
    description: str | None = Field(default=None, max_length=255)


class FeatureFlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    key: str
    enabled: bool
    description: str | None = None


class WebhookIn(BaseModel):
    event_type: str = Field(min_length=1, max_length=48)
    url: str = Field(min_length=1, max_length=512)
    secret: str | None = Field(default=None, max_length=128)
    active: bool = True


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_type: str
    url: str
    active: bool


class WebhookPatch(BaseModel):
    event_type: str | None = Field(default=None, min_length=1, max_length=48)
    url: str | None = Field(default=None, min_length=1, max_length=512)
    secret: str | None = Field(default=None, max_length=128)
    active: bool | None = None

    _v_nulls = reject_nulls("event_type", "url", "active")


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    webhook_id: int
    event_type: str
    status: str
    attempts: int
    response_code: int | None = None


class ProductSearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sku: str
    name: str
    category: str | None = None
    status: str


# ---------- Audit log ----------
@router.get("/audit-log", response_model=list[AuditLogOut], operation_id="list_audit_log")
async def list_audit_log(
    action: str | None = None,
    admin_user_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(AdminAuditLog).order_by(AdminAuditLog.id.desc())
    if action:
        q = q.where(AdminAuditLog.action == action)
    if admin_user_id is not None:
        q = q.where(AdminAuditLog.admin_user_id == admin_user_id)
    return await paged(db, q, page)


# ---------- CSV exports ----------
@router.get("/exports/{kind}.csv", operation_id="export_csv", response_class=Response)
async def export_csv(
    kind: str, request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if kind not in _EXPORT_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown export")
    data, truncated = await svc.export_csv(db, kind)
    await svc.record_audit(
        db, user.id, "export", kind, None,
        ip=client_ip(request),
    )
    await db.commit()
    return Response(
        content=data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{kind}.csv"',
            "X-Export-Truncated": "true" if truncated else "false",
        },
    )


# ---------- Feature flags ----------
@router.get("/feature-flags", response_model=list[FeatureFlagOut], operation_id="list_feature_flags")
async def list_feature_flags(db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(FeatureFlag).order_by(FeatureFlag.key).limit(500))).scalars().all()


@router.put("/feature-flags", response_model=FeatureFlagOut, operation_id="upsert_feature_flag")
async def upsert_feature_flag(data: FeatureFlagIn, db: AsyncSession = Depends(get_db)):
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == data.key))
    if flag is None:
        # Concurrent first-upserts for the same key collide on UNIQUE(key); create in a
        # savepoint and re-select on collision so the second request updates, not 500s.
        try:
            async with db.begin_nested():
                flag = FeatureFlag(
                    key=data.key, enabled=data.enabled, description=data.description
                )
                db.add(flag)
                await db.flush()
        except IntegrityError:
            # Unique violation means the conflicting txn committed its row — re-select is
            # guaranteed to find it; scalar_one so a miss fails loudly, not via refresh(None).
            flag = (
                await db.execute(select(FeatureFlag).where(FeatureFlag.key == data.key))
            ).scalar_one()
    if flag is not None and (flag.enabled != data.enabled or data.description is not None):
        flag.enabled = data.enabled
        if data.description is not None:
            flag.description = data.description
    await db.commit()
    await db.refresh(flag)
    return flag


@router.get("/feature-flags/{key}", response_model=FeatureFlagOut, operation_id="get_feature_flag")
async def get_feature_flag(key: str, db: AsyncSession = Depends(get_db)):
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == key))
    if flag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flag not found")
    return flag


# ---------- Outbound webhooks ----------
@router.post("/outbound-webhooks", response_model=WebhookOut, operation_id="create_outbound_webhook")
async def create_outbound_webhook(data: WebhookIn, db: AsyncSession = Depends(get_db)):
    hook = OutboundWebhook(**data.model_dump())
    db.add(hook)
    await db.commit()
    await db.refresh(hook)
    return hook


@router.get("/outbound-webhooks", response_model=list[WebhookOut], operation_id="list_outbound_webhooks")
async def list_outbound_webhooks(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    return await paged(db, select(OutboundWebhook).order_by(OutboundWebhook.id.desc()), page)


@router.delete(
    "/outbound-webhooks/{hook_id}", status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_outbound_webhook",
)
async def delete_outbound_webhook(hook_id: int, db: AsyncSession = Depends(get_db)):
    hook = await db.get(OutboundWebhook, hook_id)
    if hook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    await db.delete(hook)
    await db.commit()


@router.patch(
    "/outbound-webhooks/{hook_id}", response_model=WebhookOut, operation_id="update_outbound_webhook"
)
async def update_outbound_webhook(hook_id: int, data: WebhookPatch, db: AsyncSession = Depends(get_db)):
    hook = await db.get(OutboundWebhook, hook_id)
    if hook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(hook, field, value)
    await db.commit()
    await db.refresh(hook)
    return hook


@router.get(
    "/outbound-webhooks/{hook_id}/deliveries",
    response_model=list[WebhookDeliveryOut],
    operation_id="list_webhook_deliveries",
)
async def list_webhook_deliveries(
    hook_id: int, db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)
):
    if await db.get(OutboundWebhook, hook_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    q = (
        select(OutboundWebhookDelivery)
        .where(OutboundWebhookDelivery.webhook_id == hook_id)
        .order_by(OutboundWebhookDelivery.id.desc())
    )
    return await paged(db, q, page)


@router.post(
    "/outbound-webhooks/{hook_id}/test",
    response_model=WebhookDeliveryOut,
    operation_id="test_outbound_webhook",
)
async def test_outbound_webhook(hook_id: int, db: AsyncSession = Depends(get_db)):
    """Fire a test delivery at this webhook (uses the real dispatch, fail-soft)."""
    hook = await db.get(OutboundWebhook, hook_id)
    if hook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    payload = {"event": hook.event_type, "test": True, "ts": dt.datetime.now(dt.UTC).isoformat()}
    delivery = OutboundWebhookDelivery(
        webhook_id=hook.id, event_type=hook.event_type, payload=payload, status="pending"
    )
    db.add(delivery)
    # Commit the pending row first: the HTTP call (up to 10s) then runs with no
    # transaction open, then the outcome is recorded.
    await db.commit()
    await svc._attempt_delivery(hook, delivery, payload)
    await db.commit()
    await db.refresh(delivery)
    return delivery


# ---------- Admin product list ----------
@router.get("/products", response_model=list[ProductOut], operation_id="admin_list_products")
async def admin_list_products(
    status_filter: str = Query("all", alias="status", pattern="^(all|active|retired)$"),
    q: str | None = None,
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    """Catalogue list for the admin dashboard. Unlike the storefront `/products`
    (active only, cached) this can include retired products and reports a total."""
    query = select(Product).order_by(Product.id.desc())
    if status_filter != "all":
        query = query.where(Product.status == status_filter)
    if category is not None:
        query = query.where(Product.category == category)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        query = query.where(
            or_(Product.name.ilike(like, escape="\\"), Product.sku.ilike(like, escape="\\"))
        )
    return await paged(db, query, page)


# ---------- Full-text product search ----------
@router.get("/products/search", response_model=list[ProductSearchOut], operation_id="admin_product_search")
async def product_search(
    q: str, db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)
):
    # Postgres full-text over name+sku+description (GIN expression index in migration).
    stmt = (
        select(Product)
        .where(
            text(
                "to_tsvector('english', coalesce(name,'')||' '||coalesce(sku,'')||' '"
                "||coalesce(description,'')) @@ websearch_to_tsquery('english', :q)"
            )
        )
        .order_by(Product.id)
        .params(q=q)
    )
    return await paged(db, stmt, page)


# ---------- Admin product detail ----------
# Declared after /products/search so the literal path wins over the int parameter.
@router.get(
    "/products/{product_id}", response_model=ProductOut, operation_id="admin_get_product",
    responses={404: {"description": "Product not found"}},
)
async def admin_get_product(product_id: int, db: AsyncSession = Depends(get_db)):
    """One product for the admin detail screen. Unlike the storefront GET /products/{id},
    retired products are returned too (their status says so)."""
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product
