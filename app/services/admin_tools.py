"""Admin tooling services: audit trail, feature flags, outbound webhooks, CSV exports."""
import asyncio
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
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


_DEV_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _resolve_public(url: str) -> tuple[str, int, str] | None:
    """SSRF guard: only http(s), and EVERY resolved IP must be globally routable
    (`is_global` also rejects CGNAT 100.64/10 — e.g. Alibaba metadata 100.100.100.200 —
    and other special ranges that is_private misses). Returns (hostname, port, ip) to
    CONNECT TO, so the request can't be re-resolved to an internal address afterwards
    (DNS rebinding); None if blocked."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except Exception:
        return None
    ips = []
    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return None
        if not ip.is_global or ip.is_multicast:
            return None
        ips.append(str(ip))
    if not ips:
        return None
    return parsed.hostname, port, ips[0]


def _is_public_url(url: str) -> bool:
    return _resolve_public(url) is not None


def _is_dev_url(url: str) -> bool:
    """Dev/local sink (no network call) — never in prod, and only EXACT local hosts (the
    old prefix match 'http://dev' also caught real hosts like developer.partner.com)."""
    if settings.ENV == "prod":
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in _DEV_HOSTS or host.endswith(".localhost")


async def _attempt_delivery(hook: OutboundWebhook, delivery: OutboundWebhookDelivery, payload: dict) -> None:
    delivery.attempts += 1
    body = json.dumps(payload, default=str).encode()
    # No real URL (dev/local) -> mark sent without a network call.
    if not hook.url or _is_dev_url(hook.url):
        delivery.status = "sent"
        delivery.response_code = 200
        return
    # SSRF guard: never let an admin-configured URL reach an internal/metadata address.
    target = await asyncio.to_thread(_resolve_public, hook.url)
    if target is None:
        log.warning("outbound webhook blocked (non-public host) for hook %s", hook.id)
        delivery.status = "failed"
        delivery.response_code = 0
        return
    hostname, _port, ip = target
    try:
        import httpx

        headers = {"Content-Type": "application/json", "X-DRS-Event": hook.event_type,
                   "Host": hostname}
        if hook.secret:
            headers["X-DRS-Signature"] = hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
        # Connect to the IP we just validated (TLS still verifies the real hostname via
        # SNI), and never follow redirects (a 30x could point at an internal address).
        pinned = httpx.URL(hook.url).copy_with(host=ip)
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
            resp = await c.post(
                pinned, content=body, headers=headers,
                extensions={"sni_hostname": hostname},
            )
        delivery.response_code = resp.status_code
        delivery.status = "sent" if resp.status_code < 300 else "failed"
    except Exception as exc:
        log.warning("outbound webhook delivery failed: %s", exc)
        delivery.status = "failed"


def _csv_safe(v):
    """Neutralize spreadsheet formula injection: a text cell starting with = + - @ (or a
    tab/CR) is prefixed with a quote so Excel/Sheets won't execute it on open."""
    if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


# Hard cap so an admin export can't load an unbounded table into memory. At the cap the
# CSV is truncated (newest ids beyond the cap are omitted); a full dump is an offline job.
_EXPORT_MAX_ROWS = 100_000


def _rows_to_csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows([[_csv_safe(c) for c in row] for row in rows])
    return buf.getvalue()


async def export_csv(db: AsyncSession, kind: str) -> str:
    if kind == "orders":
        rows = (
            await db.execute(select(Order).order_by(Order.id).limit(_EXPORT_MAX_ROWS))
        ).scalars().all()
        header = ["id", "order_no", "account_id", "status", "payment_status",
                  "subtotal", "discount", "total"]
        data = [[o.id, o.order_no, o.account_id, o.status, o.payment_status,
                 float(o.subtotal), float(o.discount), float(o.total)] for o in rows]
    elif kind == "payments":
        rows = (
            await db.execute(select(Payment).order_by(Payment.id).limit(_EXPORT_MAX_ROWS))
        ).scalars().all()
        header = ["id", "account_id", "payment_type", "gateway", "status", "amount", "created_at"]
        data = [[p.id, p.account_id, p.payment_type, p.gateway, p.status,
                 float(p.amount), p.created_at] for p in rows]
    elif kind == "users":
        rows = (
            await db.execute(select(User).order_by(User.id).limit(_EXPORT_MAX_ROWS))
        ).scalars().all()
        header = ["id", "phone", "email", "role", "status", "created_at"]
        data = [[u.id, u.phone, u.email, u.role, u.status, u.created_at] for u in rows]
    elif kind == "invoices":
        rows = (
            await db.execute(select(Invoice).order_by(Invoice.id).limit(_EXPORT_MAX_ROWS))
        ).scalars().all()
        header = ["id", "invoice_no", "fy", "account_id", "invoice_date", "taxable_value",
                  "cgst", "sgst", "igst", "total", "is_credit_note"]
        data = [[i.id, i.invoice_no, i.fy, i.account_id, i.invoice_date, float(i.taxable_value),
                 float(i.cgst), float(i.sgst), float(i.igst), float(i.total), i.is_credit_note]
                for i in rows]
    else:
        raise ValueError(f"unknown export kind: {kind}")
    # The CSV string-build is CPU-bound — keep it off the event loop so a large export
    # doesn't stall every other request on the worker.
    return await asyncio.to_thread(_rows_to_csv, header, data)
