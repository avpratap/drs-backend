#!/bin/sh
# Render (and any Docker host) entrypoint for the DRS API service.
# Render execs the service command as argv without a shell, so multi-step
# startup (migrate -> seed -> serve) lives here and is invoked as `sh start.sh`.
set -e

# Apply database migrations (idempotent).
uv run alembic upgrade head

# Seed demo data. app/seed.py is idempotent and refuses ENV=prod, so this is a
# no-op in production and safe to run on every (re)start in dev/staging.
uv run python -m app.seed || echo "seed skipped or failed (non-fatal): continuing"

# Storefront demo content: product photos + hero/banners/journal. Stores the baked
# images (scripts/demo_assets) into the media store and sets product image_urls. The
# media store is the local dir in stub mode, which is ephemeral on Render free, so this
# re-runs on every boot (it's idempotent and refuses ENV=prod). Without it, products
# have no image_url and the storefront shows the fallback illustration.
uv run python scripts/seed_storefront_demo.py || echo "storefront demo seed skipped or failed (non-fatal): continuing"

# Serve. Render provides $PORT (defaults to 10000); fall back to 8000 locally.
exec uv run uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --no-proxy-headers
