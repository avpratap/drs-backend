"""Scheduled jobs without an ARQ worker (Render free tier): POST /api/v1/internal/jobs/{job}
guarded by JOBS_TOKEN, the scripts/run_job.py CLI, and the config pieces behind them."""
import datetime as dt
import importlib.util
import uuid
from pathlib import Path

import pytest
from conftest import auth, login
from sqlalchemy import select

from app.core.config import Settings, settings
from app.core.db import SessionLocal
from app.models.delivery import DeliveryOrder
from app.services.subscription import today_ist

TOKEN = "t" * 40
URL = "/api/v1/internal/jobs"


@pytest.fixture
def jobs_on(monkeypatch):
    monkeypatch.setattr(settings, "JOBS_TOKEN", TOKEN)
    return {"Authorization": f"Bearer {TOKEN}"}


async def test_disabled_without_a_token(client, monkeypatch):
    monkeypatch.setattr(settings, "JOBS_TOKEN", "")
    r = await client.post(f"{URL}/outbox", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404


async def test_rejects_missing_or_wrong_token(client, jobs_on):
    assert (await client.post(f"{URL}/outbox")).status_code == 401
    bad = {"Authorization": "Bearer " + "x" * 40}
    assert (await client.post(f"{URL}/outbox", headers=bad)).status_code == 401
    assert (await client.post(f"{URL}/outbox", headers={"Authorization": TOKEN})).status_code == 401
    # An admin's login is not a jobs token either.
    admin = auth(await login(client, "9999900001"))
    assert (await client.post(f"{URL}/outbox", headers=admin)).status_code == 401


async def test_cutoff_runs_inline_and_is_idempotent(client, jobs_on):
    ah = auth(await login(client, "9999900001"))
    shop = (await client.post("/api/v1/shops", json={"name": f"Job {uuid.uuid4().hex[:6]}"},
                              headers=ah)).json()["id"]
    sid = (await client.post("/api/v1/subscriptions", json={"shop_id": shop}, headers=ah)).json()["id"]
    await client.put(f"/api/v1/subscriptions/{sid}/items",
                     json=[{"product_id": 1, "default_qty": 2, "frequency": "daily"}], headers=ah)
    date = "2037-03-03"
    r = await client.post(f"{URL}/cutoff?date={date}", headers=jobs_on)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job"] == "cutoff" and body["ok"] is True
    assert body["result"]["delivery_date"] == date and body["result"]["orders_generated"] >= 1
    again = (await client.post(f"{URL}/cutoff?date={date}", headers=jobs_on)).json()
    assert again["ok"] is True
    async with SessionLocal() as db:  # still exactly one order for the shop
        rows = (await db.execute(select(DeliveryOrder).where(
            DeliveryOrder.shop_id == shop,
            DeliveryOrder.delivery_date == dt.date.fromisoformat(date)))).scalars().all()
    assert len(rows) == 1


async def test_cutoff_defaults_to_tomorrow_and_refuses_today(client, jobs_on):
    r = await client.post(f"{URL}/cutoff", headers=jobs_on)
    assert r.status_code == 200
    assert r.json()["result"]["delivery_date"] == str(today_ist() + dt.timedelta(days=1))
    today = today_ist().isoformat()
    assert (await client.post(f"{URL}/cutoff?date={today}", headers=jobs_on)).status_code == 422


async def test_partial_cutoff_reports_not_ok(client, jobs_on, monkeypatch):
    from app.services import jobs as jobs_svc

    async def partial(db, d):
        return {"schedule_run_id": 1, "delivery_date": d, "orders_generated": 3, "failed": 2}

    monkeypatch.setattr(jobs_svc, "run_cutoff", partial)
    body = (await client.post(f"{URL}/cutoff?date=2037-04-04", headers=jobs_on)).json()
    assert body["ok"] is False and body["result"]["failed"] == 2


@pytest.mark.parametrize("job", ["outbox", "housekeeping", "churn", "expire_batches"])
async def test_other_jobs_run(client, jobs_on, job):
    r = await client.post(f"{URL}/{job}", headers=jobs_on)
    assert r.status_code == 200, r.text
    assert r.json()["job"] == job and r.json()["ok"] is True
    assert isinstance(r.json()["result"], dict)


async def test_bad_job_or_date(client, jobs_on):
    assert (await client.post(f"{URL}/nope", headers=jobs_on)).status_code == 422
    assert (await client.post(f"{URL}/outbox?date=2037-01-01", headers=jobs_on)).status_code == 422


def _cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_job.py"
    spec = importlib.util.spec_from_file_location("run_job", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_cli_runs_a_job_and_reports_bad_input(capsys):
    cli = _cli()
    assert await cli.main(["housekeeping"], dispose=False) == 0
    assert '"job": "housekeeping"' in capsys.readouterr().out
    assert await cli.main(["cutoff", "--date", today_ist().isoformat()], dispose=False) == 2


def test_prod_guard_requires_a_long_jobs_token():
    from test_config_guard import _GOOD

    assert Settings(**_GOOD).JOBS_TOKEN == ""  # optional
    assert Settings(**_GOOD, JOBS_TOKEN="y" * 32).JOBS_TOKEN
    with pytest.raises(Exception, match="JOBS_TOKEN"):
        Settings(**_GOOD, JOBS_TOKEN="short")


def test_database_url_gets_the_asyncpg_driver():
    assert Settings(DATABASE_URL="postgresql://u:p@h:5432/d").DATABASE_URL == \
        "postgresql+asyncpg://u:p@h:5432/d"
    assert Settings(DATABASE_URL="postgres://u@h/d").DATABASE_URL == "postgresql+asyncpg://u@h/d"
    already = "postgresql+asyncpg://u@h/d"
    assert Settings(DATABASE_URL=already).DATABASE_URL == already
