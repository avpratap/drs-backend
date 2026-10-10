import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import require_role
from app.schemas.delivery import CutoffResult
from app.services.cutoff import run_cutoff
from app.services.subscription import today_ist

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@router.post("/cutoff/run", response_model=CutoffResult, operation_id="run_cutoff")
async def run_cutoff_endpoint(date: dt.date | None = None, db: AsyncSession = Depends(get_db)):
    """Materialize active subscriptions into delivery_orders for a date (default tomorrow)."""
    delivery_date = date or (today_ist() + dt.timedelta(days=1))
    return await run_cutoff(db, delivery_date)
