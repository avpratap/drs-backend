"""Content / CMS API: admin authoring, public reads, and SEO (sitemap/robots).

Three routers:
  * `router`            — admin CRUD, mount under /api/v1 (paths /admin/content/*).
  * `public_router`     — public reads, mount under /api/v1 (paths /content/*).
  * `public_root_router`— sitemap.xml + robots.txt, mount on the APP ROOT (no prefix).
"""
import datetime as dt
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import require_role
from app.deps.pagination import Page, pagination
from app.models.content import Article, Banner, StaticPage
from app.services import content as content_svc


def _safe_web_url(v: str | None) -> str | None:
    """Reject javascript:/data: (stored-XSS) — allow only http(s) or a relative path."""
    if v is None:
        return v
    if v.strip().lower().startswith(("http://", "https://", "/")):
        return v
    raise ValueError("URL must be http(s) or a relative path")

router = APIRouter(prefix="/admin/content", tags=["content"], dependencies=[Depends(require_role("admin"))])
public_router = APIRouter(prefix="/content", tags=["content-public"])
public_root_router = APIRouter(tags=["seo"])


# ---------- Schemas ----------
# body_html is admin-authored CMS HTML served via the PUBLIC /content router. It is NOT
# sanitized server-side (that would strip legitimate markup); the web frontend MUST render
# it through an HTML sanitizer / strict CSP. Bounded here as defense-in-depth.
_BODY_MAX = 100_000


class ArticleIn(BaseModel):
    slug: str = Field(min_length=1, max_length=160, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=255)
    body_html: str = Field(min_length=1, max_length=_BODY_MAX)
    excerpt: str | None = Field(default=None, max_length=500)
    author_name: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = Field(default=None, max_length=30)
    meta_title: str | None = Field(default=None, max_length=255)
    meta_description: str | None = Field(default=None, max_length=320)
    cover_image_url: str | None = Field(default=None, max_length=512)


class ArticleUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    body_html: str | None = Field(default=None, max_length=_BODY_MAX)
    excerpt: str | None = Field(default=None, max_length=500)
    author_name: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = Field(default=None, max_length=30)
    meta_title: str | None = Field(default=None, max_length=255)
    meta_description: str | None = Field(default=None, max_length=320)
    cover_image_url: str | None = Field(default=None, max_length=512)


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    slug: str
    title: str
    body_html: str
    excerpt: str | None = None
    author_name: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    status: str
    meta_title: str | None = None
    meta_description: str | None = None
    cover_image_url: str | None = None
    published_at: dt.datetime | None = None


class PageIn(BaseModel):
    slug: str = Field(min_length=1, max_length=160, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=255)
    body_html: str = Field(min_length=1, max_length=_BODY_MAX)
    meta_title: str | None = Field(default=None, max_length=255)
    meta_description: str | None = Field(default=None, max_length=320)


class PageUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    body_html: str | None = Field(default=None, max_length=_BODY_MAX)
    meta_title: str | None = Field(default=None, max_length=255)
    meta_description: str | None = Field(default=None, max_length=320)


class PageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    slug: str
    title: str
    body_html: str
    status: str
    meta_title: str | None = None
    meta_description: str | None = None
    published_at: dt.datetime | None = None


class BannerIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    image_url: str = Field(min_length=1, max_length=512)
    cta_link: str | None = Field(default=None, max_length=512)
    position: int = 0
    active: bool = True
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None

    _v_urls = field_validator("image_url", "cta_link")(lambda v: _safe_web_url(v))


class BannerUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    image_url: str | None = Field(default=None, min_length=1, max_length=512)
    cta_link: str | None = Field(default=None, max_length=512)
    position: int | None = None
    active: bool | None = None
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None

    _v_urls = field_validator("image_url", "cta_link")(lambda v: _safe_web_url(v))


class BannerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    image_url: str
    cta_link: str | None = None
    position: int
    active: bool
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None


async def _slug_free(db: AsyncSession, model, slug: str) -> None:
    exists = await db.scalar(select(model.id).where(model.slug == slug))
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already in use")


# ---------- Admin: articles ----------
@router.post("/articles", response_model=ArticleOut, operation_id="create_article")
async def create_article(data: ArticleIn, db: AsyncSession = Depends(get_db)):
    await _slug_free(db, Article, data.slug)
    art = Article(**data.model_dump())
    db.add(art)
    try:
        await db.commit()
    except IntegrityError:  # concurrent create of the same slug
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already in use") from None
    await db.refresh(art)
    return art


@router.get("/articles", response_model=list[ArticleOut], operation_id="admin_list_articles")
async def admin_list_articles(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Article).order_by(Article.id.desc())
    if status_filter:
        q = q.where(Article.status == status_filter)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


async def _get_article(db: AsyncSession, article_id: int) -> Article:
    art = await db.get(Article, article_id)
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")
    return art


@router.patch("/articles/{article_id}", response_model=ArticleOut, operation_id="update_article")
async def update_article(article_id: int, data: ArticleUpdate, db: AsyncSession = Depends(get_db)):
    art = await _get_article(db, article_id)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(art, k, v)
    await db.commit()
    await db.refresh(art)
    return art


@router.post("/articles/{article_id}/publish", response_model=ArticleOut, operation_id="publish_article")
async def publish_article(article_id: int, db: AsyncSession = Depends(get_db)):
    art = await _get_article(db, article_id)
    content_svc.publish(art)
    await db.commit()
    await db.refresh(art)
    return art


@router.post("/articles/{article_id}/unpublish", response_model=ArticleOut, operation_id="unpublish_article")
async def unpublish_article(article_id: int, db: AsyncSession = Depends(get_db)):
    art = await _get_article(db, article_id)
    content_svc.unpublish(art)
    await db.commit()
    await db.refresh(art)
    return art


@router.delete("/articles/{article_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_article")
async def delete_article(article_id: int, db: AsyncSession = Depends(get_db)):
    art = await _get_article(db, article_id)
    await db.delete(art)
    await db.commit()


# ---------- Admin: static pages ----------
@router.post("/pages", response_model=PageOut, operation_id="create_page")
async def create_page(data: PageIn, db: AsyncSession = Depends(get_db)):
    await _slug_free(db, StaticPage, data.slug)
    page_obj = StaticPage(**data.model_dump())
    db.add(page_obj)
    try:
        await db.commit()
    except IntegrityError:  # concurrent create of the same slug
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already in use") from None
    await db.refresh(page_obj)
    return page_obj


@router.get("/pages", response_model=list[PageOut], operation_id="admin_list_pages")
async def admin_list_pages(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    return (
        await db.execute(select(StaticPage).order_by(StaticPage.id.desc()).limit(page.limit).offset(page.offset))
    ).scalars().all()


async def _get_page(db: AsyncSession, page_id: int) -> StaticPage:
    obj = await db.get(StaticPage, page_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    return obj


@router.patch("/pages/{page_id}", response_model=PageOut, operation_id="update_page")
async def update_page(page_id: int, data: PageUpdate, db: AsyncSession = Depends(get_db)):
    obj = await _get_page(db, page_id)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.post("/pages/{page_id}/publish", response_model=PageOut, operation_id="publish_page")
async def publish_page(page_id: int, db: AsyncSession = Depends(get_db)):
    obj = await _get_page(db, page_id)
    content_svc.publish(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.post("/pages/{page_id}/unpublish", response_model=PageOut, operation_id="unpublish_page")
async def unpublish_page(page_id: int, db: AsyncSession = Depends(get_db)):
    obj = await _get_page(db, page_id)
    content_svc.unpublish(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/pages/{page_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_page")
async def delete_page(page_id: int, db: AsyncSession = Depends(get_db)):
    obj = await _get_page(db, page_id)
    await db.delete(obj)
    await db.commit()


# ---------- Admin: banners ----------
@router.post("/banners", response_model=BannerOut, operation_id="create_banner")
async def create_banner(data: BannerIn, db: AsyncSession = Depends(get_db)):
    b = Banner(**data.model_dump())
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return b


@router.get("/banners", response_model=list[BannerOut], operation_id="admin_list_banners")
async def admin_list_banners(db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)):
    return (
        await db.execute(select(Banner).order_by(Banner.position, Banner.id).limit(page.limit).offset(page.offset))
    ).scalars().all()


@router.patch("/banners/{banner_id}", response_model=BannerOut, operation_id="update_banner")
async def update_banner(banner_id: int, data: BannerUpdate, db: AsyncSession = Depends(get_db)):
    b = await db.get(Banner, banner_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Banner not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(b, k, v)
    await db.commit()
    await db.refresh(b)
    return b


@router.delete("/banners/{banner_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="delete_banner")
async def delete_banner(banner_id: int, db: AsyncSession = Depends(get_db)):
    b = await db.get(Banner, banner_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Banner not found")
    await db.delete(b)
    await db.commit()


# ---------- Public reads ----------
@public_router.get("/articles", response_model=list[ArticleOut], operation_id="list_public_articles")
async def list_public_articles(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(Article).where(Article.status == "published").order_by(Article.published_at.desc().nullslast(), Article.id.desc())
    if category:
        q = q.where(Article.category == category)
    return (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()


@public_router.get("/articles/{slug}", response_model=ArticleOut, operation_id="get_public_article")
async def get_public_article(slug: str, db: AsyncSession = Depends(get_db)):
    art = await db.scalar(select(Article).where(Article.slug == slug, Article.status == "published"))
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")
    return art


@public_router.get("/pages/{slug}", response_model=PageOut, operation_id="get_public_page")
async def get_public_page(slug: str, db: AsyncSession = Depends(get_db)):
    obj = await db.scalar(select(StaticPage).where(StaticPage.slug == slug, StaticPage.status == "published"))
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    return obj


@public_router.get("/banners", response_model=list[BannerOut], operation_id="list_public_banners")
async def list_public_banners(db: AsyncSession = Depends(get_db)):
    return await content_svc.active_banners(db)


# ---------- SEO (mount on app root) ----------
@public_root_router.get("/sitemap.xml", operation_id="sitemap_xml", include_in_schema=False)
async def sitemap_xml(db: AsyncSession = Depends(get_db)):
    entries = await content_svc.build_sitemap(db)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for e in entries:
        parts.append("<url><loc>" + escape(e["loc"]) + "</loc>" + (f"<lastmod>{e['lastmod']}</lastmod>" if e["lastmod"] else "") + "</url>")
    parts.append("</urlset>")
    return Response(content="".join(parts), media_type="application/xml")


@public_root_router.get("/robots.txt", operation_id="robots_txt", include_in_schema=False)
async def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        f"Sitemap: {content_svc.SITE_BASE_URL}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")
