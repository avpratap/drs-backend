import math

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import settings

app = FastAPI(title="DRS API", version="0.1.0")

# Production safety guards: fail fast instead of booting an insecure instance.
if settings.ENV == "prod":
    if "dev-only" in settings.JWT_SECRET or len(settings.JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must be a strong secret (>=32 chars) in prod")
    if "*" in settings.cors_origins_list:
        raise RuntimeError("CORS_ORIGINS must be an explicit allow-list in prod")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": _sanitize(exc.errors())},
    )


app.include_router(health_router)  # /healthz, /readyz
app.include_router(api_router)  # /api/v1/...


@app.get("/")
async def root():
    return {"app": "DRS API", "env": settings.ENV, "docs": "/docs"}
