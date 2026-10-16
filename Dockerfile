# DRS backend — FastAPI API + ARQ worker share this image.
# API:    (default CMD) uvicorn
# Worker: override CMD -> ["uv","run","arq","app.worker.WorkerSettings"]
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first (cached layer) from the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App code.
COPY . .
RUN uv sync --frozen --no-dev

# Drop root: run as an unprivileged user in production.
RUN useradd -r -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Container-level healthcheck hits the liveness probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# --proxy-headers so X-Forwarded-For is honoured behind Railway/Fly.
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
