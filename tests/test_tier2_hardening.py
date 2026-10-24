"""Tests for Tier-2/3 audit hardening fixes."""
import pytest
import sqlalchemy as sa
from conftest import auth, login
from sqlalchemy.exc import DBAPIError

from app.core.phone import normalize_phone


def test_normalize_phone_canonical_forms():
    assert normalize_phone("+91 98765-43210") == "9876543210"
    assert normalize_phone("919876543210") == "9876543210"
    assert normalize_phone("09876543210") == "9876543210"
    assert normalize_phone("9876543210") == "9876543210"


async def test_phone_dedupe_same_user(client):
    """+91-prefixed and bare forms of one number resolve to a single account."""
    from app.core.redis import redis_client

    a = await login(client, "+918877665544")
    id_a = (await client.get("/api/v1/auth/me", headers=auth(a))).json()["id"]
    # Same normalized number shares the OTP cooldown key — clear it to log in again.
    await redis_client.delete("otp:cooldown:8877665544")
    b = await login(client, "8877665544")
    id_b = (await client.get("/api/v1/auth/me", headers=auth(b))).json()["id"]
    assert id_a == id_b


async def test_error_envelope_has_code(client):
    admin = await login(client, "9999900001")
    r = await client.get("/api/v1/admin/purchase-orders/999999", headers=auth(admin))
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] and body["error_code"] == "not_found"


async def test_webhook_events_immutable(client):
    """block-UPDATE trigger: a recorded gateway event cannot be altered."""
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO webhook_events (event_id, event_type, payload, created_at) "
                "VALUES ('evt_t2', 'payment.captured', '{}', now())"
            )
        )
    with pytest.raises(DBAPIError) as ei:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE webhook_events SET event_type='x' WHERE event_id='evt_t2'")
            )
    assert "row is immutable" in str(ei.value)
    # DELETE is still allowed (housekeeping prunes old events).
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM webhook_events WHERE event_id='evt_t2'"))


async def test_stock_floor_check(client):
    """stock_items.qty_on_hand has a floor (can go slightly negative, not unbounded)."""
    from app.core.db import engine

    async with engine.begin() as conn:
        pid = (await conn.execute(sa.text("SELECT id FROM products LIMIT 1"))).scalar()
        loc = (await conn.execute(sa.text("SELECT id FROM locations LIMIT 1"))).scalar()
    with pytest.raises(DBAPIError) as ei:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO stock_items (product_id, location_id, qty_on_hand, reorder_point, updated_at) "
                    "VALUES (:p, :l, -2000000, 0, now())"
                ),
                {"p": pid, "l": loc},
            )
    assert "ck_stock_qty_floor" in str(ei.value)


async def test_negative_balance_blocked(client):
    """wallet_adjust rejects an over-debit unless allow_negative is set."""
    from app.core.db import SessionLocal
    from app.models.shop import CustomerAccount
    from app.models.user import User
    from app.services import billing

    async with SessionLocal() as db:
        # Fresh user+account => guaranteed zero wallet (no cross-test funding).
        user = User(phone="9100000099", role="customer", status="active")
        db.add(user)
        await db.flush()
        acct = CustomerAccount(
            owner_user_id=user.id, account_type="retail", billing_mode="prepaid_wallet"
        )
        db.add(acct)
        await db.flush()
        await billing.wallet_adjust(db, acct.id, 50, "topup", "test", 1)
        with pytest.raises(billing.InsufficientBalance):  # overdraw must be refused
            await billing.wallet_adjust(db, acct.id, -100, "debit", "test", 2)
        await db.rollback()  # never commit test rows


async def test_metrics_exposes_latency_histogram(client):
    await client.get("/api/v1/products")  # generate a timed request
    body = (await client.get("/metrics")).text
    assert "http_request_duration_ms_bucket" in body
    assert 'le="+Inf"' in body


async def test_money_check_blocks_negative_unit_price(client):
    from app.core.db import engine

    async with engine.begin() as conn:
        pid = (await conn.execute(sa.text("SELECT id FROM products LIMIT 1"))).scalar()
        acct = (await conn.execute(sa.text("SELECT id FROM customer_accounts LIMIT 1"))).scalar()
        shop = (await conn.execute(sa.text("SELECT id FROM shops LIMIT 1"))).scalar()
        await conn.execute(
            sa.text(
                "INSERT INTO orders (order_no, account_id, shop_id, order_type, status, "
                "subtotal, discount, total, payment_status, created_at, updated_at) "
                "VALUES ('T2CHK', :a, :s, 'one_time', 'created', 0, 0, 0, 'pending', now(), now())"
            ),
            {"a": acct, "s": shop},
        )
        oid = (await conn.execute(sa.text("SELECT id FROM orders WHERE order_no='T2CHK'"))).scalar()
    try:
        with pytest.raises(DBAPIError) as ei:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO order_items (order_id, product_id, qty, unit_price, line_total) "
                        "VALUES (:o, :p, 1, -5, 0)"
                    ),
                    {"o": oid, "p": pid},
                )
        assert "ck_order_items_price_nonneg" in str(ei.value)
    finally:  # cleanup even when an assert fails (no leaked test rows)
        async with engine.begin() as conn:
            await conn.execute(sa.text("DELETE FROM orders WHERE id = :o"), {"o": oid})
