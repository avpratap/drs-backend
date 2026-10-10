# DRS Backend — Scaling & Resilience (Phase 2)

What's implemented and how to turn it on in production.

## Connection pooling (PgBouncer)
asyncpg + a transaction-pooling PgBouncer must **not** use server-side prepared
statements. The app handles this via `DB_POOL_MODE`:

- `direct` (default, dev): app-side pool (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, recycle 30m).
- `pgbouncer` (prod): `NullPool` + `statement_cache_size=0`.

To run locally with PgBouncer:
```
docker compose --profile prod up -d        # starts db, redis, pgbouncer:6432
# .env:
DB_POOL_MODE=pgbouncer
DATABASE_URL=postgresql+asyncpg://drs:drs@localhost:6432/drs
```
On Railway/Fly point `DATABASE_URL` at the managed PgBouncer / pooled port.

## Caching
Redis cache-aside (`app/core/cache.py`), fail-open. Applied to `GET /products`
(invalidated on product writes). TTL via `CACHE_TTL_SECONDS` (default 300s).
Extend to price tiers / shop lists the same way.

## Idempotency
Clients send `Idempotency-Key: <uuid>` on writes. First 2xx is stored and replayed
on retry (`Idempotent-Replay: true`); non-2xx releases the key; key reuse with a
different body is a 422. Table: `idempotency_keys`. Prune old rows periodically
(e.g. `DELETE ... WHERE created_at < now() - interval '48 hours'`).

## Background jobs (ARQ)
`max_tries=4`, `retry_jobs=True`, `job_timeout=600s`, `keep_result=1h`. The nightly
cutoff is chunked (200 subs/commit) under a session-level advisory lock; a
concurrent run returns `{skipped: "locked"}`. The notification outbox is drained
every minute (`drain_outbox`) and marks rows `failed` after `MAX_ATTEMPTS` (the
dead-letter surface — query `notifications WHERE status='failed'`).

## Observability
- `/metrics` — Prometheus text (requests by method/status + latency). Scrape it.
- `X-Request-ID` / `X-Response-Time-ms` on every response; structured request log.
- `LOG_JSON=true` for JSON logs; `SENTRY_DSN=...` to enable Sentry (needs sentry-sdk).

## Partitioning (deferred to deploy-time)
`delivery_orders` and `notifications` grow fastest. At MVP data volumes partitioning
is premature and converting a populated table in place is disruptive, so it is **not**
applied yet. When row counts warrant it (≈ tens of millions), do it as a maintenance
migration:
1. Create `delivery_orders_p` as `PARTITION BY RANGE (delivery_date)` with the same
   columns + a monthly partition creation routine (pg_partman or a cron that
   pre-creates next month's partition).
2. Backfill by copying data; swap names in a transaction; recreate FKs/indexes.
3. Keep `UNIQUE(shop_id, delivery_date)` as a per-partition constraint.

Track this in the `drs-deploy` skill checklist.
