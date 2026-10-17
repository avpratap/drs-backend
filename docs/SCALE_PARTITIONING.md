# DRS Backend — Partitioning & Scale Runbook (#41)

This is the **plan to execute when row counts warrant it** (roughly tens of millions
of rows, or when index bloat / vacuum time hurts). Do NOT run these migrations early —
declarative partitioning requires recreating the table, which is a heavy online
operation. Until then the existing indexes + short-retention pruning
(`app/services/maintenance.prune`) keep things healthy.

## What to partition (and why these)

| Table | Growth driver | Partition key | Strategy |
|-------|---------------|---------------|----------|
| `delivery_orders` | ~1 row / shop / day | `delivery_date` | RANGE by month |
| `stock_movements` | every receipt/sale/transfer | `created_at` | RANGE by month (append-only — can't prune) |
| `notifications` | every event × channel | `created_at` | RANGE by month (short-retention already pruned) |
| `wallet_transactions`, `credit_ledger`, `points_ledger`, `gift_card_transactions` | every money move | `created_at` | RANGE by month (append-only ledgers — retained forever, so partition) |
| `shipment_tracking_events` | every courier scan | `created_at` | RANGE by month |

Append-only ledgers are protected by the `drs_append_only` trigger (no UPDATE/DELETE),
so you **cannot** prune them — partitioning is the only way to keep them fast. Dropping
an old *partition* is a metadata operation and does not violate the row-level trigger,
but for financial/GST records you normally **detach-and-archive** rather than drop.

## Online conversion recipe (native declarative partitioning, PG16)

Do this per table during a low-traffic window; it is backward-compatible if done in the
documented order (the app keeps reading/writing the same table name throughout).

```sql
-- 1. New partitioned parent (same columns), e.g. for stock_movements:
CREATE TABLE stock_movements_p (LIKE stock_movements INCLUDING ALL) PARTITION BY RANGE (created_at);

-- 2. Monthly partitions covering existing + near-future data (script this).
CREATE TABLE stock_movements_p_2026_10 PARTITION OF stock_movements_p
  FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
-- ... one per month ...
CREATE TABLE stock_movements_p_default PARTITION OF stock_movements_p DEFAULT;

-- 3. Backfill (batched to avoid a long lock):
INSERT INTO stock_movements_p SELECT * FROM stock_movements;  -- or batched by id range

-- 4. Swap names in one transaction:
BEGIN;
ALTER TABLE stock_movements RENAME TO stock_movements_old;
ALTER TABLE stock_movements_p RENAME TO stock_movements;
COMMIT;

-- 5. Recreate the append-only trigger + FKs/indexes on the new parent, verify, then
--    drop stock_movements_old once reconciled.
```

Prefer **`pg_partman`** to automate monthly partition creation + retention/detach, run
from a nightly maintenance job (extend `app/worker.py` housekeeping once partitioning is live).

## Alembic notes
- Each conversion is its own migration with a real `downgrade()` (rename back).
- Re-apply the `drs_append_only` trigger to the new parent in the same migration.
- The CI `alembic upgrade → downgrade -1 → upgrade` gate still applies — test the
  round-trip on a seeded copy before production.

## Cursor pagination (for very large lists)
Offset pagination (`app/deps/pagination.py`) is fine to ~100k rows per result set. Beyond
that, deep offsets get slow. For the big append-only lists (movements, tracking,
notifications, ledgers) switch the list endpoints to **keyset/cursor** pagination:
`WHERE id < :cursor ORDER BY id DESC LIMIT :n`, returning the last id as the next cursor.
This is an additive, per-endpoint change — do it for an endpoint only when it actually
serves large pages.

## Read replicas (RDS at scale)
Route read-only analytics/report/export queries (admin reports, CSV exports, churn
scoring, forecasting history reads) to a **read replica** via a second async engine
(`DATABASE_REPLICA_URL`), keeping the primary for writes + money paths. Money paths and
anything inside a `SELECT ... FOR UPDATE` transaction MUST stay on the primary.

## Trigger order for a zero-downtime rollout
1. Ship pruning + indexes (done).
2. Add `pg_partman` + partition the newest-growing table first (`notifications` or
   `stock_movements`) and observe.
3. Partition `delivery_orders` (the query hot-path for delivery/cutoff) next.
4. Partition the ledgers last (most sensitive; detach-archive, never drop).
5. Add a read replica and move reporting/exports/ML reads onto it.
