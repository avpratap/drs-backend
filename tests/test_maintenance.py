import datetime as dt

import pytest
from conftest import auth, idem, login
from sqlalchemy import select, text

from app.core.db import SessionLocal
from app.models.idempotency import IdempotencyKey
from app.services.maintenance import prune


async def test_prune_removes_old_idempotency_keys():
    async with SessionLocal() as db:
        old = IdempotencyKey(
            key="old", method="POST", path="/x", request_hash="h",
            created_at=dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
        )
        fresh = IdempotencyKey(
            key="fresh", method="POST", path="/y", request_hash="h",
            created_at=dt.datetime.now(dt.UTC),
        )
        db.add_all([old, fresh])
        await db.commit()

        counts = await prune(db)
        assert counts["idempotency_keys"] >= 1

        remaining = {
            k.key for k in (await db.execute(select(IdempotencyKey))).scalars()
        }
        assert "old" not in remaining and "fresh" in remaining


async def test_wallet_ledger_is_append_only(client):
    admin = await login(client, "9999900001")
    sub = await login(client, "9999900002")
    acct = (await client.get("/api/v1/billing/me", headers=auth(sub))).json()["account_id"]
    await client.post(
        "/api/v1/billing/admin/topup",
        json={"account_id": acct, "amount": 10},
        headers=idem(auth(admin)),
    )
    # The DB trigger must reject any UPDATE/DELETE on the ledger.
    async with SessionLocal() as db:
        with pytest.raises(Exception):  # noqa: B017 — asyncpg raises on the trigger
            await db.execute(text("UPDATE wallet_transactions SET amount = amount + 1"))
            await db.commit()
