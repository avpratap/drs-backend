"""Shared list pagination: `?limit=&offset=`, bounded so no endpoint can be made
to return an unbounded result set.

Admin list endpoints also report the unpaginated row count in an `X-Total-Count`
response header (via `paged` / `set_total`) so dashboards can render page counts
without changing the list-shaped response bodies existing clients rely on."""
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import Query, Response
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
TOTAL_COUNT_HEADER = "X-Total-Count"


@dataclass
class Page:
    limit: int
    offset: int
    response: Response | None = field(default=None, repr=False, compare=False)


def pagination(
    response: Response,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Page:
    return Page(limit=limit, offset=offset, response=response)


async def set_total(db: AsyncSession, query: Select, page: Page) -> int:
    """Count the rows `query` matches (ignoring ordering/pagination) and expose the
    count as `X-Total-Count` on the response."""
    total = await db.scalar(
        select(func.count()).select_from(query.order_by(None).limit(None).offset(None).subquery())
    )
    total = int(total or 0)
    if page.response is not None:
        page.response.headers[TOTAL_COUNT_HEADER] = str(total)
    return total


async def paged(db: AsyncSession, query: Select, page: Page) -> Sequence[Any]:
    """Run `query` for one page of ORM rows, setting `X-Total-Count` first."""
    await set_total(db, query, page)
    return (await db.execute(query.limit(page.limit).offset(page.offset))).scalars().all()
