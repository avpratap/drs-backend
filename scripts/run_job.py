"""Run one scheduled job once and exit (for a platform cron, e.g. the Render cron job in
render.yaml): same code as the ARQ worker and POST /api/v1/internal/jobs/{job}.

Run:  uv run python scripts/run_job.py cutoff [--date YYYY-MM-DD]
      uv run python scripts/run_job.py {outbox|housekeeping|churn|expire_batches}
Exit status: 0 ok, 1 ran but not fully ok (e.g. a partial cutoff), 2 bad input.
"""
import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `app` when run by path


async def main(argv: list[str] | None = None, *, dispose: bool = True) -> int:
    from app.core.db import SessionLocal, engine
    from app.core.redis import redis_client
    from app.services import jobs

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job", choices=jobs.JOB_NAMES)
    ap.add_argument("--date", type=dt.date.fromisoformat, help="cutoff only: delivery date")
    args = ap.parse_args(argv)
    try:
        async with SessionLocal() as db:
            ok, result = await jobs.run_job(db, args.job, args.date)
    except jobs.JobInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if dispose:  # one-shot process: close pools cleanly (tests reuse them)
            await engine.dispose()
            await redis_client.aclose()
    print(json.dumps({"job": args.job, "ok": ok, "result": result}, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
