from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete, cache_get_json, cache_set_json
from app.core.db import get_db
from app.deps.auth import get_current_user, get_optional_user, require_role
from app.deps.pagination import Page, pagination
from app.models.product import Product
from app.models.rating import Rating, ReviewHelpful
from app.models.user import User
from app.models.variant import ProductVariant
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate
from app.services import reviews as review_svc

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
    status: str
    verified_purchase: bool
    helpful_count: int
    admin_reply: str | None = None


class ProductRatingsOut(BaseModel):
    product_id: int
    count: int
    average: float
    distribution: dict[int, int]
    ratings: list[RatingOut]


class HelpfulOut(BaseModel):
    rating_id: int
    helpful_count: int
    voted: bool


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
    verified = await review_svc.is_verified_purchase(db, user.id, product_id)
    # One rating per user per product — upsert.
    existing = await db.scalar(
        select(Rating).where(Rating.user_id == user.id, Rating.product_id == product_id)
    )
    if existing is not None:
        existing.stars = data.stars
        existing.comment = data.comment
        existing.verified_purchase = verified
        row = existing
    else:
        row = Rating(
            user_id=user.id, product_id=product_id, stars=data.stars, comment=data.comment,
            verified_purchase=verified,
        )
        db.add(row)
    await db.flush()
    await review_svc.recompute_product_rating(db, product_id)
    await db.commit()
    await cache_delete(_CACHE_KEY)
    await db.refresh(row)
    return row


@router.delete(
    "/{product_id}/ratings",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_my_rating",
)
async def delete_my_rating(
    product_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    row = await db.scalar(
        select(Rating).where(Rating.user_id == user.id, Rating.product_id == product_id)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    await db.delete(row)
    await db.flush()
    await review_svc.recompute_product_rating(db, product_id)
    await db.commit()
    await cache_delete(_CACHE_KEY)


@router.post(
    "/{product_id}/ratings/{rating_id}/helpful",
    response_model=HelpfulOut,
    operation_id="toggle_review_helpful",
)
async def toggle_helpful(
    product_id: int,
    rating_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(select(Rating).where(Rating.id == rating_id).with_for_update())
    ).scalar_one_or_none()
    if row is None or row.product_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    vote = await db.scalar(
        select(ReviewHelpful).where(
            ReviewHelpful.rating_id == rating_id, ReviewHelpful.user_id == user.id
        )
    )
    if vote is None:
        db.add(ReviewHelpful(rating_id=rating_id, user_id=user.id))
        row.helpful_count += 1
        voted = True
    else:
        await db.delete(vote)
        row.helpful_count = max(0, row.helpful_count - 1)
        voted = False
    await db.commit()
    return HelpfulOut(rating_id=rating_id, helpful_count=row.helpful_count, voted=voted)


@router.get(
    "/{product_id}/ratings", operation_id="list_product_ratings", response_model=ProductRatingsOut
)
async def list_product_ratings(
    product_id: int,
    sort: str = "recent",
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    # Distribution + aggregate over APPROVED reviews only (what the public sees).
    dist_rows = (
        await db.execute(
            select(Rating.stars, func.count())
            .where(Rating.product_id == product_id, Rating.status == "approved")
            .group_by(Rating.stars)
        )
    ).all()
    distribution = dict.fromkeys(range(1, 6), 0)
    total = 0
    weighted = 0
    for stars, n in dist_rows:
        distribution[int(stars)] = int(n)
        total += int(n)
        weighted += int(stars) * int(n)
    average = round(weighted / total, 2) if total else 0.0

    # Non-admins see approved reviews plus their own (any status); admins see all.
    q = select(Rating).where(Rating.product_id == product_id)
    if user is None:
        q = q.where(Rating.status == "approved")
    elif user.role != "admin":
        q = q.where(or_(Rating.status == "approved", Rating.user_id == user.id))
    q = q.order_by(
        Rating.helpful_count.desc() if sort == "helpful" else Rating.id.desc()
    )
    rows = (await db.execute(q.limit(page.limit).offset(page.offset))).scalars().all()
    return {
        "product_id": product_id,
        "count": total,
        "average": average,
        "distribution": distribution,
        "ratings": [RatingOut.model_validate(r).model_dump() for r in rows],
    }


# ---------- Variants (catalog/display only; checkout stays product-level) ----------
class VariantIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    pack_size: float = Field(default=1, gt=0, le=1_000_000, allow_inf_nan=False)
    price: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)
    mrp: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    image_url: str | None = Field(default=None, max_length=512)
    sort_order: int = 0


class VariantUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    pack_size: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    price: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    mrp: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    image_url: str | None = Field(default=None, max_length=512)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")
    sort_order: int | None = None


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    sku: str
    label: str
    pack_size: float
    price: float
    mrp: float | None = None
    image_url: str | None = None
    status: str
    sort_order: int


class ProductFullOut(ProductOut):
    variants: list[VariantOut] = []


async def _require_product(db: AsyncSession, product_id: int) -> Product:
    product = await db.get(Product, product_id)
    if product is None or product.status == "retired":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


@router.post(
    "/{product_id}/variants",
    response_model=VariantOut,
    operation_id="create_variant",
    dependencies=[Depends(require_role("admin"))],
)
async def create_variant(product_id: int, data: VariantIn, db: AsyncSession = Depends(get_db)):
    await _require_product(db, product_id)
    variant = ProductVariant(product_id=product_id, **data.model_dump())
    db.add(variant)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Variant SKU already exists") from None
    await db.refresh(variant)
    return variant


@router.get(
    "/{product_id}/variants", response_model=list[VariantOut], operation_id="list_variants"
)
async def list_variants(
    product_id: int,
    all: bool = False,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(ProductVariant).where(ProductVariant.product_id == product_id)
    # Only admins may request inactive variants via ?all=true; everyone else sees active.
    if not (all and user is not None and user.role == "admin"):
        q = q.where(ProductVariant.status == "active")
    q = q.order_by(ProductVariant.sort_order, ProductVariant.id).limit(200)
    return (await db.execute(q)).scalars().all()


@router.get("/{product_id}/full", response_model=ProductFullOut, operation_id="get_product_full")
async def get_product_full(product_id: int, db: AsyncSession = Depends(get_db)):
    product = await _require_product(db, product_id)
    variants = (
        await db.execute(
            select(ProductVariant)
            .where(ProductVariant.product_id == product_id, ProductVariant.status == "active")
            .order_by(ProductVariant.sort_order, ProductVariant.id)
        )
    ).scalars().all()
    out = ProductFullOut.model_validate(product)
    out.variants = [VariantOut.model_validate(v) for v in variants]
    return out


@router.patch(
    "/variants/{variant_id}",
    response_model=VariantOut,
    operation_id="update_variant",
    dependencies=[Depends(require_role("admin"))],
)
async def update_variant(variant_id: int, data: VariantUpdate, db: AsyncSession = Depends(get_db)):
    variant = await db.get(ProductVariant, variant_id)
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Variant not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(variant, field, value)
    await db.commit()
    await db.refresh(variant)
    return variant


@router.delete(
    "/variants/{variant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="retire_variant",
    dependencies=[Depends(require_role("admin"))],
)
async def retire_variant(variant_id: int, db: AsyncSession = Depends(get_db)):
    variant = await db.get(ProductVariant, variant_id)
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Variant not found")
    variant.status = "inactive"  # soft-delete
    await db.commit()
