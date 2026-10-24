"""Content/CMS helpers: publish lifecycle, published listings, sitemap data."""
import datetime as dt
import os
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_get_json, cache_set_json
from app.core.config import settings
from app.models.content import Article, Banner, StaticPage

# Public site origin used to build absolute sitemap URLs (web app domain).
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://drs.co.in").rstrip("/")

# Static routes worth advertising in the sitemap beyond DB-backed content.
_STATIC_ROUTES = ("/", "/shop", "/blog")


def publish(obj) -> None:
    obj.status = "published"
    if obj.published_at is None:
        obj.published_at = dt.datetime.now(dt.UTC)


def unpublish(obj) -> None:
    obj.status = "draft"
    obj.published_at = None


async def active_banners(db: AsyncSession) -> list[Banner]:
    # Gate on the business-local (IST) date, not the server's UTC date — a banner set to
    # start/end "today" must flip at local midnight, not at 05:30 IST.
    today = dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    rows = (
        await db.execute(
            select(Banner)
            .where(
                Banner.active.is_(True),
                or_(Banner.valid_from.is_(None), Banner.valid_from <= today),
                or_(Banner.valid_to.is_(None), Banner.valid_to >= today),
            )
            .order_by(Banner.position, Banner.id)
            .limit(200)  # bounded: homepage banner set is small
        )
    ).scalars().all()
    return rows


SITEMAP_MAX_URLS = 50_000  # the sitemap protocol's per-file limit
_SITEMAP_CACHE_KEY = "sitemap:v1"


async def build_sitemap(db: AsyncSession) -> list[dict]:
    """Public + unauthenticated: select only slug/dates (never the up-to-100 KB bodies),
    cap the rows, and cache the result briefly so repeated hits don't hammer the DB."""
    cached = await cache_get_json(_SITEMAP_CACHE_KEY)
    if cached is not None:
        return cached
    entries: list[dict] = [{"loc": f"{SITE_BASE_URL}{r}", "lastmod": None} for r in _STATIC_ROUTES]
    for model, prefix in ((Article, "/blog/"), (StaticPage, "/")):
        rows = (
            await db.execute(
                select(model.slug, model.published_at, model.updated_at)
                .where(model.status == "published")
                .order_by(model.id)
                .limit(SITEMAP_MAX_URLS)
            )
        ).all()
        for slug, published_at, updated_at in rows:
            lastmod = published_at or updated_at
            entries.append({
                "loc": f"{SITE_BASE_URL}{prefix}{slug}",
                "lastmod": lastmod.date().isoformat() if lastmod else None,
            })
    entries = entries[:SITEMAP_MAX_URLS]
    await cache_set_json(_SITEMAP_CACHE_KEY, entries, ttl=300)
    return entries
