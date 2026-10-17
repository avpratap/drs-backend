from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import redis_client

router = APIRouter(tags=["health"])


@router.get("/healthz", operation_id="healthz")
async def healthz():
    """Liveness — the process is up."""
    return {"status": "ok"}


@router.get("/readyz", operation_id="readyz")
async def readyz(db: AsyncSession = Depends(get_db)):
    """Readiness — can reach Postgres + Redis."""
    checks: dict[str, str] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    ok = all(v == "ok" for v in checks.values())
    payload = {"status": "ok" if ok else "degraded", "checks": checks}
    # Readiness probes key off the HTTP status, not the body — return 503 when a
    # dependency is down so the orchestrator pulls the pod out of rotation.
    return JSONResponse(payload, status_code=200 if ok else 503)
