import math
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.db import engine
from app.core.idempotency import IdempotencyMiddleware
from app.core.observability import (
    RequestContextMiddleware,
    configure_logging,
    init_sentry,
    metrics_response,
)
from app.core.ratelimit import RateLimitMiddleware
from app.core.redis import redis_client

configure_logging()
init_sentry()

# Production safety guards: fail fast instead of booting an insecure instance.
if settings.ENV == "prod":
    if "dev-only" in settings.JWT_SECRET or len(settings.JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must be a strong secret (>=32 chars) in prod")
    if "*" in settings.cors_origins_list:
        raise RuntimeError("CORS_ORIGINS must be an explicit allow-list in prod")
    # Payment secrets: without these the gateway stub accepts the "dev" signature
    # and unsigned webhooks — forgeable payments. Refuse to boot.
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        raise RuntimeError("RAZORPAY_KEY_ID/SECRET required in prod")
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        raise RuntimeError("RAZORPAY_WEBHOOK_SECRET required in prod")
    # GST tax invoices are not legally valid without the seller's GSTIN.
    if not settings.SELLER_GSTIN:
        raise RuntimeError("SELLER_GSTIN required in prod (GST tax invoices)")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Drain connections cleanly on SIGTERM (Railway/Fly redeploys).
    await engine.dispose()
    await redis_client.aclose()


# Hide API docs in prod (don't advertise the surface publicly).
_docs = None if settings.ENV == "prod" else "/docs"
app = FastAPI(
    title="DRS API", version="0.1.0", docs_url=_docs, redoc_url=None, lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RateLimitMiddleware)  # reject abusive IPs before heavy work
app.add_middleware(RequestContextMiddleware)  # outermost: times & tags every request


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-XSS-Protection", "0")
    if settings.ENV == "prod":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    return response


def _sanitize(obj):
    """Coerce non-finite floats (inf/nan) so the 422 body is JSON-serializable."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _error_code(status_code: int) -> str:
    """Stable machine-readable code, e.g. 404 -> 'not_found'."""
    try:
        return HTTPStatus(status_code).name.lower()
    except ValueError:
        return "error"


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": _sanitize(exc.errors()), "error_code": "unprocessable_entity"},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Consistent error envelope ({detail, error_code}) across the whole API while
    # preserving the existing `detail` key and any auth headers (e.g. WWW-Authenticate).
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "error_code": _error_code(exc.status_code)},
        headers=getattr(exc, "headers", None),
    )


app.include_router(health_router)  # /healthz, /readyz
app.include_router(api_router)  # /api/v1/...


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()


# Dev-stub object retrieval. In prod, R2/CDN serves these instead.
from app.core.storage import enabled as _storage_enabled  # noqa: E402

if not _storage_enabled():
    import os

    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @app.get("/files/{key:path}", include_in_schema=False)
    async def serve_file(key: str):
        safe = os.path.normpath(key).replace("\\", "/")
        if safe.startswith("..") or os.path.isabs(safe):
            raise HTTPException(status_code=404)
        path = os.path.join(settings.STORAGE_DIR, safe)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404)
        return FileResponse(path)


@app.get("/")
async def root():
    return {"app": "DRS API", "env": settings.ENV, "docs": "/docs"}
