"""Shared list pagination: `?limit=&offset=`, bounded so no endpoint can be made
to return an unbounded result set."""
from dataclasses import dataclass

from fastapi import Query

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass
class Page:
    limit: int
    offset: int


def pagination(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Page:
    return Page(limit=limit, offset=offset)
