"""Admin depth: audit log, CSV exports, feature flags, outbound webhooks, product search."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, pagination
from app.models.admin_tools import AdminAuditLog, FeatureFlag, OutboundWebhook
from app.models.product import Product
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
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


# ---------- CSV exports ----------
@router.get("/exports/{kind}.csv", operation_id="export_csv", response_class=Response)
async def export_csv(
    kind: str, request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if kind not in _EXPORT_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown export")
    data = await svc.export_csv(db, kind)
    await svc.record_audit(
        db, user.id, "export", kind, None,
        ip=request.client.host if request.client else None,
    )
    await db.commit()
    return Response(
        content=data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
    )


# ---------- Feature flags ----------
@router.get("/feature-flags", response_model=list[FeatureFlagOut], operation_id="list_feature_flags")
async def list_feature_flags(db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(FeatureFlag).order_by(FeatureFlag.key))).scalars().all()


@router.put("/feature-flags", response_model=FeatureFlagOut, operation_id="upsert_feature_flag")
async def upsert_feature_flag(data: FeatureFlagIn, db: AsyncSession = Depends(get_db)):
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == data.key))
    if flag is None:
        flag = FeatureFlag(key=data.key, enabled=data.enabled, description=data.description)
        db.add(flag)
    else:
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
    return (
        await db.execute(
            select(OutboundWebhook).order_by(OutboundWebhook.id.desc()).limit(page.limit).offset(page.offset)
        )
    ).scalars().all()


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
        .limit(page.limit)
        .offset(page.offset)
    )
    return (await db.execute(stmt, {"q": q})).scalars().all()
