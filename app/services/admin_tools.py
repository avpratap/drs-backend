"""Admin tooling services: audit trail, feature flags, outbound webhooks, CSV exports."""
import csv
import hashlib
import hmac
import io
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_tools import (
    AdminAuditLog,
    FeatureFlag,
    OutboundWebhook,
    OutboundWebhookDelivery,
)
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User

log = logging.getLogger("drs.admin_tools")


async def record_audit(
    db: AsyncSession, admin_id: int, action: str, resource_type=None, resource_id=None,
    detail: dict | None = None, ip: str | None = None,
) -> AdminAuditLog:
    row = AdminAuditLog(
        admin_user_id=admin_id, action=action, resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        detail=detail, ip=ip,
    )
    db.add(row)
    await db.flush()
    return row


async def is_enabled(db: AsyncSession, key: str, default: bool = False) -> bool:
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == key))
    return flag.enabled if flag is not None else default


async def enqueue_outbound(db: AsyncSession, event_type: str, payload: dict) -> int:
    """Create a delivery row per active webhook subscribed to this event, then attempt
    delivery fail-soft. Returns how many deliveries were queued."""
    hooks = (
        await db.execute(
            select(OutboundWebhook).where(
                OutboundWebhook.event_type == event_type, OutboundWebhook.active.is_(True)
            )
        )
    ).scalars().all()
    for hook in hooks:
        delivery = OutboundWebhookDelivery(
            webhook_id=hook.id, event_type=event_type, payload=payload, status="pending"
        )
        db.add(delivery)
        await db.flush()
        await _attempt_delivery(hook, delivery, payload)
    return len(hooks)


async def _attempt_delivery(hook: OutboundWebhook, delivery: OutboundWebhookDelivery, payload: dict) -> None:
    delivery.attempts += 1
    body = json.dumps(payload, default=str).encode()
    # No real URL (dev/local) -> mark sent without a network call.
    if not hook.url or hook.url.startswith(("http://localhost", "https://localhost", "http://dev", "https://dev")):
        delivery.status = "sent"
        delivery.response_code = 200
        return
    try:
        import httpx

        headers = {"Content-Type": "application/json", "X-DRS-Event": hook.event_type}
        if hook.secret:
            headers["X-DRS-Signature"] = hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(hook.url, content=body, headers=headers)
        delivery.response_code = resp.status_code
        delivery.status = "sent" if resp.status_code < 300 else "failed"
    except Exception as exc:
        log.warning("outbound webhook delivery failed: %s", exc)
        delivery.status = "failed"


def _rows_to_csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


async def export_csv(db: AsyncSession, kind: str) -> str:
    if kind == "orders":
        rows = (await db.execute(select(Order).order_by(Order.id))).scalars().all()
        return _rows_to_csv(
            ["id", "order_no", "account_id", "status", "payment_status", "subtotal", "discount", "total"],
            [[o.id, o.order_no, o.account_id, o.status, o.payment_status,
              float(o.subtotal), float(o.discount), float(o.total)] for o in rows],
        )
    if kind == "payments":
        rows = (await db.execute(select(Payment).order_by(Payment.id))).scalars().all()
        return _rows_to_csv(
            ["id", "account_id", "payment_type", "gateway", "status", "amount", "created_at"],
            [[p.id, p.account_id, p.payment_type, p.gateway, p.status, float(p.amount), p.created_at]
             for p in rows],
        )
    if kind == "users":
        rows = (await db.execute(select(User).order_by(User.id))).scalars().all()
        return _rows_to_csv(
            ["id", "phone", "email", "role", "status", "created_at"],
            [[u.id, u.phone, u.email, u.role, u.status, u.created_at] for u in rows],
        )
    if kind == "invoices":
        rows = (await db.execute(select(Invoice).order_by(Invoice.id))).scalars().all()
        return _rows_to_csv(
            ["id", "invoice_no", "fy", "account_id", "invoice_date", "taxable_value",
             "cgst", "sgst", "igst", "total", "is_credit_note"],
            [[i.id, i.invoice_no, i.fy, i.account_id, i.invoice_date, float(i.taxable_value),
              float(i.cgst), float(i.sgst), float(i.igst), float(i.total), i.is_credit_note]
             for i in rows],
        )
    raise ValueError(f"unknown export kind: {kind}")
