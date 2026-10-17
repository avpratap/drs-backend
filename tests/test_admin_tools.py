"""Tier-4 #37 admin depth: audit log, CSV exports, feature flags, outbound webhooks, search."""
from conftest import auth, login


async def test_feature_flags_upsert_read_and_is_enabled(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    up = await client.put("/api/v1/admin/feature-flags", json={"key": "new_checkout", "enabled": True, "description": "soft launch"}, headers=h)
    assert up.status_code == 200 and up.json()["enabled"] is True
    got = await client.get("/api/v1/admin/feature-flags/new_checkout", headers=h)
    assert got.status_code == 200 and got.json()["enabled"] is True

    # is_enabled service reflects the stored flag.
    from app.core.db import SessionLocal
    from app.services import admin_tools as svc

    async with SessionLocal() as db:
        assert await svc.is_enabled(db, "new_checkout") is True
        assert await svc.is_enabled(db, "missing_flag") is False

    # Toggle off.
    off = await client.put("/api/v1/admin/feature-flags", json={"key": "new_checkout", "enabled": False}, headers=h)
    assert off.json()["enabled"] is False
    assert any(f["key"] == "new_checkout" for f in (await client.get("/api/v1/admin/feature-flags", headers=h)).json())


async def test_csv_export_and_audit_log(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    r = await client.get("/api/v1/admin/exports/orders.csv", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    first_line = r.text.splitlines()[0]
    assert "order_no" in first_line and "total" in first_line
    # The export recorded an audit-log entry.
    logs = (await client.get("/api/v1/admin/audit-log?action=export", headers=h)).json()
    assert any(entry["action"] == "export" and entry["resource_type"] == "orders" for entry in logs)

    assert (await client.get("/api/v1/admin/exports/nonsense.csv", headers=h)).status_code == 404


async def test_outbound_webhook_crud_and_enqueue(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    created = await client.post(
        "/api/v1/admin/outbound-webhooks",
        json={"event_type": "order.created", "url": "http://localhost/hook", "active": True},
        headers=h,
    )
    assert created.status_code == 200
    hook_id = created.json()["id"]
    assert any(w["id"] == hook_id for w in (await client.get("/api/v1/admin/outbound-webhooks", headers=h)).json())

    # enqueue creates a delivery row (localhost URL -> marked sent, no network).
    from sqlalchemy import func, select

    from app.core.db import SessionLocal
    from app.models.admin_tools import OutboundWebhookDelivery
    from app.services import admin_tools as svc

    async with SessionLocal() as db:
        n = await svc.enqueue_outbound(db, "order.created", {"order_id": 1})
        await db.commit()
        assert n == 1
        count = await db.scalar(
            select(func.count()).select_from(OutboundWebhookDelivery).where(
                OutboundWebhookDelivery.webhook_id == hook_id
            )
        )
        assert count == 1

    d = await client.delete(f"/api/v1/admin/outbound-webhooks/{hook_id}", headers=h)
    assert d.status_code == 204
    assert not any(w["id"] == hook_id for w in (await client.get("/api/v1/admin/outbound-webhooks", headers=h)).json())


async def test_full_text_product_search(client):
    admin = await login(client, "9999900001")
    h = auth(admin)
    # Seeded product "Full Cream Milk 1L" should match 'milk'.
    r = await client.get("/api/v1/admin/products/search?q=milk", headers=h)
    assert r.status_code == 200
    assert any("milk" in p["name"].lower() for p in r.json())


async def test_admin_tools_require_admin(client):
    cust = await login(client, "9876543210")
    h = auth(cust)
    assert (await client.get("/api/v1/admin/audit-log", headers=h)).status_code == 403
    assert (await client.get("/api/v1/admin/feature-flags", headers=h)).status_code == 403
    assert (await client.get("/api/v1/admin/exports/orders.csv", headers=h)).status_code == 403
    assert (await client.get("/api/v1/admin/products/search?q=milk", headers=h)).status_code == 403
