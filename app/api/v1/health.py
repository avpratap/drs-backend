import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import redis_client

log = logging.getLogger("drs.health")
router = APIRouter(tags=["health"])


@router.get("/healthz", operation_id="healthz")
async def healthz():
    """Liveness — the process is up."""
    return {"status": "ok"}


@router.get(
    "/readyz", operation_id="readyz",
    responses={503: {"description": "a dependency (DB/Redis) is unreachable"}},
)
async def readyz(db: AsyncSession = Depends(get_db)):
    """Readiness — can reach Postgres + Redis."""
    checks: dict[str, str] = {}
    # Unauthenticated endpoint: report only ok/error; the raw exception (DB host, user,
    # driver internals) goes to the log, never to the caller.
    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        log.warning("readyz: db check failed: %s", exc)
        checks["db"] = "error"
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        log.warning("readyz: redis check failed: %s", exc)
        checks["redis"] = "error"

    ok = all(v == "ok" for v in checks.values())
    payload = {"status": "ok" if ok else "degraded", "checks": checks}
    # Readiness probes key off the HTTP status, not the body — return 503 when a
    # dependency is down so the orchestrator pulls the pod out of rotation.
    return JSONResponse(payload, status_code=200 if ok else 503)
