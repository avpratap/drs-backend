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

## Worker (ARQ) crons
- `scheduled_cutoff` — 17:35 UTC (23:05 IST): materialize tomorrow's delivery orders.
- `drain_outbox` — every minute: send queued notifications.
- `housekeeping` — 01:00 UTC: prune idempotency keys / webhook events / old notifications.

If `CUTOFF_HOUR` changes, recompute the UTC cron time in `app/worker.py`.
