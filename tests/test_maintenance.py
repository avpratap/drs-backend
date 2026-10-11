import datetime as dt

from sqlalchemy import select

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
