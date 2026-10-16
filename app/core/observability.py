"""Lightweight, dependency-free observability.

- request-id + latency middleware (adds X-Request-ID / X-Response-Time-ms headers)
- in-process Prometheus-style counters exposed at /metrics
- JSON or plain logging via LOG_JSON
- optional Sentry (only if SENTRY_DSN set and sentry-sdk installed)

Counters are per-process; behind multiple workers a scraper aggregates them.
"""
import json
import logging
import re
import secrets
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from app.core.config import settings

log = logging.getLogger("drs.request")

# --- metrics state ---
_req_total: dict[tuple[str, int], int] = defaultdict(int)
_latency_sum_ms: float = 0.0
_latency_count: int = 0


def configure_logging() -> None:
    handler = logging.StreamHandler()
    if settings.LOG_JSON:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91)?[6-9]\d{9}(?!\d)")  # Indian mobile, optional +91


def _scrub_pii(value):
    """Recursively redact emails / phone numbers from a Sentry event (DPDP)."""
    if isinstance(value, str):
        return _PHONE_RE.sub("[redacted-phone]", _EMAIL_RE.sub("[redacted-email]", value))
    if isinstance(value, dict):
        return {k: _scrub_pii(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_pii(v) for v in value]
    return value


def init_sentry() -> None:
    if not settings.SENTRY_DSN:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            traces_sample_rate=0.1,
            environment=settings.ENV,
            send_default_pii=False,
            before_send=lambda event, _hint: _scrub_pii(event),
        )
        log.info("Sentry initialised")
    except Exception as exc:
        log.warning("Sentry not initialised: %s", exc)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        global _latency_sum_ms, _latency_count
        rid = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _req_total[(request.method, 500)] += 1
            log.exception(
                "unhandled error",
                extra={"request_id": rid, "method": request.method, "path": request.url.path},
            )
            raise
        dur_ms = (time.perf_counter() - start) * 1000
        _req_total[(request.method, response.status_code)] += 1
        _latency_sum_ms += dur_ms
        _latency_count += 1
        response.headers["X-Request-ID"] = rid
        response.headers["X-Response-Time-ms"] = f"{dur_ms:.1f}"
        log.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(dur_ms, 1),
            },
        )
        return response


def metrics_response() -> Response:
    lines = [
        "# HELP http_requests_total Total HTTP requests by method and status.",
        "# TYPE http_requests_total counter",
    ]
    for (method, status_code), count in sorted(_req_total.items()):
        lines.append(f'http_requests_total{{method="{method}",status="{status_code}"}} {count}')
    lines += [
        "# HELP http_request_duration_ms_sum Sum of request durations in ms.",
        "# TYPE http_request_duration_ms_sum counter",
        f"http_request_duration_ms_sum {_latency_sum_ms:.1f}",
        "# HELP http_request_duration_ms_count Number of timed requests.",
        "# TYPE http_request_duration_ms_count counter",
        f"http_request_duration_ms_count {_latency_count}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")
