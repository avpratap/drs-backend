"""Content/CMS helpers: publish lifecycle, published listings, sitemap data."""
import datetime as dt
import os

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    today = dt.date.today()
    rows = (
        await db.execute(
            select(Banner)
            .where(
                Banner.active.is_(True),
                or_(Banner.valid_from.is_(None), Banner.valid_from <= today),
                or_(Banner.valid_to.is_(None), Banner.valid_to >= today),
            )
            .order_by(Banner.position, Banner.id)
        )
    ).scalars().all()
    return rows


async def build_sitemap(db: AsyncSession) -> list[dict]:
    entries: list[dict] = [{"loc": f"{SITE_BASE_URL}{r}", "lastmod": None} for r in _STATIC_ROUTES]
    arts = (
        await db.execute(
            select(Article).where(Article.status == "published").order_by(Article.id)
        )
    ).scalars().all()
    for a in arts:
        lastmod = (a.published_at or a.updated_at)
        entries.append({
            "loc": f"{SITE_BASE_URL}/blog/{a.slug}",
            "lastmod": lastmod.date().isoformat() if lastmod else None,
        })
    pages = (
        await db.execute(
            select(StaticPage).where(StaticPage.status == "published").order_by(StaticPage.id)
        )
    ).scalars().all()
    for p in pages:
        lastmod = (p.published_at or p.updated_at)
        entries.append({
            "loc": f"{SITE_BASE_URL}/{p.slug}",
            "lastmod": lastmod.date().isoformat() if lastmod else None,
        })
    return entries
