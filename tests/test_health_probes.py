"""Liveness/readiness probes: /healthz + /readyz and their /health + /ready aliases."""
import asyncio

import pytest


@pytest.mark.parametrize("path", ["/healthz", "/health"])
async def test_liveness_ok_without_auth(client, path):
    r = await client.get(path)
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/readyz", "/ready"])
async def test_readiness_ok_when_db_and_redis_up(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"db": "ok", "redis": "ok"}}


@pytest.mark.parametrize("path", ["/readyz", "/ready"])
async def test_readiness_503_when_redis_down(client, monkeypatch, path):
    from app.api.v1 import health

    async def down():
        raise ConnectionError("refused")

    monkeypatch.setattr(health.redis_client, "ping", down)
    r = await client.get(path)
    assert r.status_code == 503 and r.json()["checks"] == {"db": "ok", "redis": "error"}


async def test_readiness_503_fast_when_a_dependency_hangs(client, monkeypatch):
    from app.api.v1 import health

    async def hang():
        await asyncio.sleep(30)

    monkeypatch.setattr(health, "READY_CHECK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(health.redis_client, "ping", hang)
    r = await asyncio.wait_for(client.get("/ready"), timeout=5)  # bounded, not 30 s
    assert r.status_code == 503 and r.json()["checks"]["redis"] == "error"


async def test_probes_are_exempt_from_the_rate_limit(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MIN", 1)
    for _ in range(3):
        for path in ("/health", "/ready", "/healthz", "/readyz"):
            assert (await client.get(path)).status_code == 200


def test_aliases_stay_out_of_the_openapi_schema():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/healthz" in paths and "/readyz" in paths
    assert "/health" not in paths and "/ready" not in paths
