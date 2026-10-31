# DRS Backend — Deploy & Operations Runbook

Target: **Railway (MVP)** → **Fly.io Mumbai (scale)**. One image runs both the API
(default `CMD`) and the ARQ worker (override `CMD` → `uv run arq app.worker.WorkerSettings`).

## Prerequisites (prod env vars)
Set these as platform secrets — never commit. The app **fails to boot** in prod
without the required ones (`app/core/config.py`), with ONE error listing every missing
or unsafe setting by name, e.g.:

```
RuntimeError: Refusing to start: ENV=prod but these settings are missing or unsafe:
  - MSG91_AUTHKEY, MSG91_TEMPLATE_ID: not set: OTP SMS would never be sent and nobody could log in
  - REDIS_URL: still the local dev default (localhost)
```


- `ENV=prod`
- `JWT_SECRET` (≥32 random bytes) — **required**
- `CORS_ORIGINS` (explicit allow-list, no `*`) — **required**
- `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` — **required**
- `SELLER_GSTIN` — **required** (GST tax invoices are invalid without it)
- `MSG91_AUTHKEY`, `MSG91_TEMPLATE_ID` (DLT-approved OTP template) — **required**: without
  them OTP SMS is never sent and nobody can log in, so the app refuses to boot
- `JOBS_TOKEN` — optional; enables `POST /api/v1/internal/jobs/{job}` for an external
  scheduler (see "Scheduled jobs"). When set in prod it must be ≥ 32 chars
- `DATABASE_URL`, `REDIS_URL` — **required** (boot fails if left on the local dev defaults)
- `FCM_PROJECT_ID` + `FCM_CREDENTIALS_JSON` — set **both or neither** (one alone fails
  boot); with neither, push is not sent (dev stub) and boot logs a warning
- `DB_POOL_MODE=pgbouncer` (behind PgBouncer transaction pooling) and `DB_SSL=true`
  (managed Postgres requires TLS)
- Integration keys as they come online: `MSG91_SENDER`, `GUPSHUP_*`, `ANTHROPIC_API_KEY`,
  `R2_*`, `SENTRY_DSN`.
- **PostgreSQL 15 or newer** (migration 0051 uses `NULLS NOT DISTINCT`); CI/compose run 16.
- `ALLOW_NEGATIVE_STOCK=false` — **production sets this to false.** Any delivery or stock
  consume that would take on-hand below zero is then refused with **409** ("Not enough
  stock for product … N on hand, M needed") instead of silently backordering; nothing of
  the refused delivery is billed or committed. The default (`true`) keeps the legacy
  backorder behaviour for dev/tests. Before flipping it, make sure receipts (GRN /
  batches / transfers) are recorded promptly, or drivers will be blocked at the door.
- Global rate limit (`RATE_LIMIT_PER_MIN`, default 300/min per IP): authenticated
  **admin** requests are exempt (verified against the DB: active admin, current token),
  so bulk admin/batch work isn't throttled; anonymous and non-admin traffic keeps the limit.
- `TRUSTED_PROXY_HOPS`: the number of reverse proxies in front of the app that append to
  `X-Forwarded-For`; the Dockerfile sets `1` (one platform proxy). With `0` behind a
  proxy, every user would share the proxy IP's rate limits and OTP caps, so **prod
  refuses to boot with `0`** unless `ALLOW_DIRECT_CLIENT_IP=true` states the app is
  exposed directly (no proxy). Never set it higher than the real hop count: the extra
  hop would be read from the client-controlled part of the header.
- `METRICS_TOKEN`: set it, and configure the scraper with `Authorization: Bearer <token>`
  (unset leaves `/metrics` open).
- One-off ops after upgrading past the review hardening: invoice PDFs rendered before
  private storage lived in the PUBLIC bucket (`invoices/...`); they are re-rendered
  privately on next access, but delete the old public objects from the bucket.

## Health probes
- **Liveness**: `GET /healthz` (alias `GET /health`) — always 200 if the process is up.
  Used by the Docker `HEALTHCHECK`.
- **Readiness**: `GET /readyz` (alias `GET /ready`) — pings Postgres + Redis, each
  bounded to 2 s, and returns 200, or 503 when either is down or hung. **Point the
  platform's traffic gate at `/readyz`**, not `/healthz`, so a booting/degraded
  instance doesn't receive traffic.
- Both are unauthenticated, exempt from the per-IP rate limit, and return no error
  details (those go to the log).

## Migrations on deploy (zero-downtime)
At **multi-instance scale** (Fly.io), run migrations as a **release/pre-deploy step
before** the new app version starts, never from each app instance's startup — otherwise
N booting instances race `alembic upgrade head`:

```
uv run alembic upgrade head
```

`fly.toml` does this via `release_command`. On **Railway MVP we run a single instance**
(`numReplicas: 1` in `railway.json`), so migrating inside `startCommand` is safe there —
but **before scaling Railway past one instance, move the migration to a Railway
Pre-Deploy Command** and drop it from `startCommand`, matching the Fly approach.

Rules that keep this safe for rolling deploys:
1. Migrations are **backward-compatible** (additive): new nullable columns / new
   tables first; make NOT-NULL or drop columns only in a *later* release once no
   running code references the old shape. (0029 followed this: backfill → NOT NULL.)
2. The app drains cleanly on `SIGTERM` (FastAPI lifespan disposes the DB engine and
   Redis pool), so Railway/Fly rolling restarts don't cut in-flight requests.
3. CI already runs `upgrade → downgrade -1 → upgrade` to prove every migration
   round-trips before merge.

## Backups / disaster recovery
- Use the managed provider's **automated daily backups + PITR** (Railway Postgres
  / Fly Postgres / AWS RDS). Verify retention ≥ 7 days and test a restore quarterly.
- Financial tables are append-only (`wallet_transactions`, `credit_ledger`,
  `consents`) and `webhook_events` is immutable (block-UPDATE trigger) — a restore
  reconstructs an accurate ledger.
- Keep `uv.lock` committed so any point-in-time image is byte-reproducible.

## Scaling notes
See `SCALING.md`. In short: PgBouncer transaction pooling (`DB_POOL_MODE=pgbouncer`,
NullPool app-side), Redis in-region, cutoff is a chunked idempotent ARQ batch, and
`/metrics` is per-process (aggregate at the Prometheus scraper across instances).

## Scheduled jobs (cutoff, notifications, housekeeping)

| Job | What | ARQ worker cron (UTC) | GitHub Actions (UTC) |
|---|---|---|---|
| `cutoff` | materialize tomorrow's standing orders into `delivery_orders` | 17:35 (23:05 IST) | 17:35 + 18:05 retry |
| `outbox` | send queued WhatsApp/SMS/push/email notifications | every minute | every 10 min |
| `expire_batches` | write off stock batches past expiry | 18:45 (00:15 IST) | 18:45 (00:15 IST) |
| `housekeeping` | prune idempotency keys, webhook events, old notifications; reward missed referrals | 01:00 | 01:00 |
| `churn` | churn scoring over all accounts | 02:00 | 02:00 |

All jobs are idempotent: a repeat or overlapping run is safe (the cutoff re-materializes
the same rows and takes a per-date Redis lock). The cutoff runs **inline** in whatever
calls it (chunked, one savepoint per subscription): `POST /api/v1/admin/cutoff/run`
(admin), `POST /api/v1/internal/jobs/cutoff` (scheduler), `scripts/run_job.py cutoff`
(platform cron) and the worker all call the same `run_cutoff`; none needs a worker.

### Free tier (Render free: API only, no workers or cron) — GitHub Actions
`.github/workflows/scheduled-jobs.yml` calls `POST /api/v1/internal/jobs/{job}` on the
live API on the schedule above. One-time setup:

1. Generate a token: `openssl rand -hex 32`.
2. Render → the API service → Environment: add `JOBS_TOKEN=<token>` (redeploys). With
   `JOBS_TOKEN` unset the endpoint is disabled (404). In prod the boot guard requires it
   to be ≥ 32 chars when set.
3. GitHub → repo Settings → Secrets and variables → Actions:
   - secret `DRS_JOBS_TOKEN` = the same token;
   - variable `DRS_API_URL` = `https://drs-backend-zvb1.onrender.com` (no trailing slash).
   Until both are set, scheduled runs are skipped with a warning, not failed.
4. Test: Actions → scheduled-jobs → Run workflow → job `outbox` (or `cutoff`). A run fails
   (and GitHub emails you) on a non-200 or when a cutoff ran partially (`"ok": false`);
   re-run it, the jobs are idempotent.

Why a dedicated token, not an admin login: admin access tokens live 15 minutes and
refresh tokens rotate on every use, so neither can sit in a CI secret.

Caveats of the free path:
- GitHub starts scheduled runs late under load (sometimes 10–30 min), hence the second
  cutoff run at 23:35 IST. Customers' standing-order changes freeze at the 23:00 cutoff
  anyway; both runs land before IST midnight.
- A free instance sleeps after ~15 idle minutes; the workflow wakes it via `/healthz`
  first. The 10-minute outbox run keeps it mostly awake, which uses ~744 of the 750
  free instance-hours a month (enough for this one service, not a second one).
- Notifications go out within ~10 minutes (not ~1) because the outbox drains every 10.
- Manual run with a date: Actions → scheduled-jobs → Run workflow → `cutoff` + date.

### Paid plan — `render.yaml` (API + ARQ worker + cutoff cron)
`render.yaml` is a Render Blueprint: the API (`sh start.sh`, health check `/readyz`), the
ARQ worker (`uv run arq app.worker.WorkerSettings`, which runs the crons in the table),
a Render cron job at 18:05 UTC running `scripts/run_job.py cutoff` as a safety net after
the worker's 23:05 IST run (exits non-zero on a partial run, so Render alerts), Key
Value (Redis, `noeviction`) and Postgres 16 (PostGIS). Secrets are `sync: false` and are
asked for on the first sync. `DATABASE_URL` comes from the database's `connectionString`
(`postgresql://` is normalized to `postgresql+asyncpg://` by the app).

When switching to it, **disable the GitHub workflow** (Actions → scheduled-jobs →
Disable) or unset `JOBS_TOKEN`, so jobs aren't triggered twice (harmless, but noisy).

If `CUTOFF_HOUR` changes, recompute the UTC cron times in `app/worker.py`,
`.github/workflows/scheduled-jobs.yml` and `render.yaml`.
