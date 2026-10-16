from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete, cache_get_json, cache_set_json
from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, pagination
from app.models.product import Product
from app.models.rating import Rating
from app.models.user import User
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate

router = APIRouter(prefix="/products", tags=["products"])

_CACHE_KEY = "cache:products:active"


class RatingIn(BaseModel):
    stars: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


class RatingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    product_id: int
    stars: int
    comment: str | None = None


class ProductRatingsOut(BaseModel):
    product_id: int
    count: int
    average: float
    ratings: list[RatingOut]


@router.get("", response_model=list[ProductOut], operation_id="list_products")
async def list_products(
    category: str | None = None,
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    # Only the plain full-active list is cached; filtered/search queries hit the DB.
    if category is None and q is None and page.offset == 0:
        cached = await cache_get_json(_CACHE_KEY)
        if cached is not None:
            return cached
    query = select(Product).where(Product.status == "active")
    if category is not None:
        query = query.where(Product.category == category)
    if q is not None:
        like = f"%{q}%"
        query = query.where(or_(Product.name.ilike(like), Product.sku.ilike(like)))
    query = query.order_by(Product.id).limit(page.limit).offset(page.offset)
    rows = (await db.execute(query)).scalars().all()
    out = [ProductOut.model_validate(p).model_dump() for p in rows]
    if category is None and q is None and page.offset == 0:
        await cache_set_json(_CACHE_KEY, out)
    return out


@router.get("/{product_id}", response_model=ProductOut, operation_id="get_product")
async def get_product(product_id: int, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if product is None or product.status == "retired":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


@router.post(
    "",
    response_model=ProductOut,
    operation_id="create_product",
    dependencies=[Depends(require_role("admin"))],
)
async def create_product(data: ProductCreate, db: AsyncSession = Depends(get_db)):
    product = Product(**data.model_dump())
    db.add(product)
    await db.commit()
    await db.refresh(product)
    await cache_delete(_CACHE_KEY)
    return product


@router.patch(
    "/{product_id}",
    response_model=ProductOut,
    operation_id="update_product",
    dependencies=[Depends(require_role("admin"))],
)
async def update_product(product_id: int, data: ProductUpdate, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    await db.commit()
    await db.refresh(product)
    await cache_delete(_CACHE_KEY)
    return product


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="retire_product",
    dependencies=[Depends(require_role("admin"))],
)
async def retire_product(product_id: int, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    product.status = "retired"  # soft-delete: keep for historical orders/invoices
    await db.commit()
    await cache_delete(_CACHE_KEY)


# ---------- Ratings ----------
@router.post("/{product_id}/ratings", response_model=RatingOut, operation_id="rate_product")
async def rate_product(
    product_id: int,
    data: RatingIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(Product, product_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    # One rating per user per product — upsert.
    existing = await db.scalar(
        select(Rating).where(Rating.user_id == user.id, Rating.product_id == product_id)
    )
    if existing is not None:
        existing.stars = data.stars
        existing.comment = data.comment
        row = existing
    else:
        row = Rating(
            user_id=user.id, product_id=product_id, stars=data.stars, comment=data.comment
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get(
    "/{product_id}/ratings", operation_id="list_product_ratings", response_model=ProductRatingsOut
)
async def list_product_ratings(
    product_id: int, db: AsyncSession = Depends(get_db), page: Page = Depends(pagination)
):
    agg = (
        await db.execute(
            select(func.count(), func.coalesce(func.avg(Rating.stars), 0)).where(
                Rating.product_id == product_id
            )
        )
    ).one()
    rows = (
        await db.execute(
            select(Rating)
            .where(Rating.product_id == product_id)
            .order_by(Rating.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
    ).scalars().all()
    return {
        "product_id": product_id,
        "count": agg[0],
        "average": round(float(agg[1]), 2),
        "ratings": [RatingOut.model_validate(r).model_dump() for r in rows],
    }
