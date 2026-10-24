import math
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import DataError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.core.bodylimit import BodySizeLimitMiddleware
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

# Production safety guards (secrets, payment/GST/courier credentials, CORS, storage)
# are enforced by Settings._prod_boot_guard at import time — shared with the worker.

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Drain connections cleanly on SIGTERM (Railway/Fly redeploys).
    await engine.dispose()
    await redis_client.aclose()


# Hide API docs in prod (don't advertise the surface publicly).
_docs = None if settings.ENV == "prod" else "/docs"
app = FastAPI(
    title="DRS API", version="0.1.0", docs_url=_docs, redoc_url=None, lifespan=lifespan,
    # Relative server so generated Dart/TS clients resolve against the host they're
    # pointed at (each client still sets its own baseURL) instead of defaulting to localhost.
    servers=[{"url": "/", "description": "same origin"}],
)

app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RateLimitMiddleware)  # reject abusive IPs before heavy work
app.add_middleware(RequestContextMiddleware)  # times & tags every request


# Hard cap on request body size (defense against a single huge-body DoS). Generous
# enough for the 5 MB image uploads + multipart framing; blocks multi-hundred-MB bodies.
# Enforced on the bytes actually received (chunked bodies carry no Content-Length).
_MAX_BODY_BYTES = 8 * 1024 * 1024
app.add_middleware(BodySizeLimitMiddleware, max_bytes=_MAX_BODY_BYTES)


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


# Registered LAST so it is the OUTERMOST middleware — error responses produced by the
# inner middleware (429 rate-limit, 413 body-size, 409/422 idempotency) then still carry
# Access-Control-* headers so a browser on an allowed origin can read them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize(obj):
    """Coerce a validation-error payload so the 422 body is always JSON-serializable:
    non-finite floats (inf/nan) and any non-JSON-native value (e.g. the original
    ValueError a model_validator raised, carried in the error `ctx`) become strings."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (str, int)) or obj is None:
        return obj
    return str(obj)


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


@app.exception_handler(IntegrityError)
async def integrity_exception_handler(request: Request, exc: IntegrityError):
    # Safety net: a constraint violation (duplicate / dangling FK / CHECK) is a client
    # conflict, not a server crash. Specific paths still validate up front; this stops
    # any missed one from surfacing as an opaque 500. The DB message is not echoed.
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "Request conflicts with existing data", "error_code": "conflict"},
    )


@app.exception_handler(DataError)
async def data_exception_handler(request: Request, exc: DataError):
    # Value out of range for its column (too long / numeric overflow): invalid input.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "A value is out of range or too long", "error_code": "unprocessable_entity"},
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_handler(request: Request, exc: ValidationError):
    # A schema built inside a handler (not request parsing) failed validation.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": _sanitize(exc.errors()), "error_code": "unprocessable_entity"},
    )


from app.api.v1.content import public_root_router  # noqa: E402

app.include_router(health_router)  # /healthz, /readyz
app.include_router(public_root_router)  # /sitemap.xml, /robots.txt (domain root)
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

    from app.core.storage import PRIVATE_PREFIX, verify_dev_signature

    @app.get("/files/{key:path}", include_in_schema=False)
    async def serve_file(key: str, exp: str | None = None, sig: str | None = None):
        safe = os.path.normpath(key).replace("\\", "/")
        if safe.startswith("..") or os.path.isabs(safe):
            raise HTTPException(status_code=404)
        # Private objects (invoices) need the short-lived signature minted after an
        # authorization check; 404 (not 403) so their existence isn't probeable.
        if safe.startswith(PRIVATE_PREFIX) and not verify_dev_signature(safe, exp, sig):
            raise HTTPException(status_code=404)
        path = os.path.join(settings.STORAGE_DIR, safe)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404)
        return FileResponse(path)


@app.get("/", include_in_schema=False)
async def root():
    return {"app": "DRS API", "env": settings.ENV, "docs": "/docs"}
