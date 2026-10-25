from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete, cache_get_json, cache_set_json
from app.core.db import get_db
from app.deps.auth import get_current_user, get_optional_user, require_role
from app.deps.pagination import DEFAULT_LIMIT, Page, pagination
from app.models.inventory import StockItem
from app.models.product import Product
from app.models.rating import Rating, ReviewHelpful
from app.models.user import User
from app.models.variant import ProductVariant
from app.schemas.common import reject_nulls
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate
from app.services import pricing
from app.services import reviews as review_svc

router = APIRouter(prefix="/products", tags=["products"])

_CACHE_KEY = "cache:products:active"


class RatingIn(BaseModel):
    stars: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


class RatingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    # Reviewer identity is PII: only populated for the review's own author or an admin.
    # Public listings get None + `mine=False` so other users can't be enumerated.
    user_id: int | None = None
    mine: bool = False
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


_SORTS = {
    "newest": Product.id.desc(),
    "rating": Product.avg_rating.desc(),
    "name": Product.name.asc(),
}


async def _from_prices(db: AsyncSession, products: list[dict]) -> dict[int, float | None]:
    """Storefront "from" price per product: min selling price over its ACTIVE variants
    (its own pack_size when it has none), from the cart's RETAIL resolver. Two queries for
    the whole page. None when no pack has a retail price."""
    ids = [p["id"] for p in products]
    if not ids:
        return {}
    variants = (
        await db.execute(
            select(ProductVariant.product_id, ProductVariant.pack_size).where(
                ProductVariant.product_id.in_(ids), ProductVariant.status == "active"
            )
        )
    ).all()
    packs_by_product: dict[int, list[float]] = {}
    for pid, pack in variants:
        packs_by_product.setdefault(pid, []).append(float(pack))
    for p in products:
        packs_by_product.setdefault(p["id"], [float(p["pack_size"])])
    prices = await pricing.retail_pack_prices(
        db, [(pid, pack) for pid, packs in packs_by_product.items() for pack in packs]
    )
    out: dict[int, float | None] = {}
    for pid, packs in packs_by_product.items():
        known = [prices[(pid, pack)] for pack in packs if prices.get((pid, pack)) is not None]
        out[pid] = min(known) if known else None
    return out


async def _with_from_prices(db: AsyncSession, products: list[dict]) -> list[dict]:
    fp = await _from_prices(db, products)
    return [{**p, "from_price": fp.get(p["id"])} for p in products]


async def _from_price_one(db: AsyncSession, product: Product, variants: list) -> float | None:
    if variants:
        known = [v.selling_price for v in variants if v.selling_price is not None]
        return min(known) if known else None
    return (await _from_prices(db, [{"id": product.id, "pack_size": product.pack_size}]))[product.id]


@router.get("", response_model=list[ProductOut], operation_id="list_products")
async def list_products(
    category: str | None = None,
    q: str | None = None,
    sort: str | None = None,  # newest | rating | name (default: id asc)
    perishable: bool | None = None,
    in_stock: bool | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    # Only the plain default-page full-active list is cached; anything else hits DB.
    # The limit MUST be in the cache condition — the cached payload is a `.limit(N)` slice,
    # so caching a non-default limit would poison the shared entry for all callers.
    plain = (
        category is None and q is None and sort is None
        and perishable is None and in_stock is None
        and page.offset == 0 and page.limit == DEFAULT_LIMIT
    )
    if plain:
        cached = await cache_get_json(_CACHE_KEY)
        if cached is not None:
            # Prices are attached fresh on every read (never cached), so a price change
            # shows immediately and the card always equals what the cart charges.
            return await _with_from_prices(db, cached)
    query = select(Product).where(Product.status == "active")
    if category is not None:
        query = query.where(Product.category == category)
    if q is not None:
        # Escape LIKE metacharacters so a `%`/`_` in the query is matched literally
        # (otherwise `q=%` matches everything and forces a full scan).
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        query = query.where(
            or_(Product.name.ilike(like, escape="\\"), Product.sku.ilike(like, escape="\\"))
        )
    if perishable is not None:
        query = query.where(Product.perishable.is_(perishable))
    if in_stock:
        in_stock_ids = (
            select(StockItem.product_id)
            .group_by(StockItem.product_id)
            .having(func.sum(StockItem.qty_on_hand) > 0)
        )
        query = query.where(Product.id.in_(in_stock_ids))
    query = query.order_by(_SORTS.get(sort, Product.id)).limit(page.limit).offset(page.offset)
    rows = (await db.execute(query)).scalars().all()
    out = [ProductOut.model_validate(p).model_dump() for p in rows]
    if plain:
        await cache_set_json(_CACHE_KEY, out)
    return await _with_from_prices(db, out)


@router.get("/categories", response_model=list[str], operation_id="list_product_categories")
async def list_product_categories(db: AsyncSession = Depends(get_db)):
    """Distinct categories among active products (storefront nav/facets)."""
    rows = (
        await db.execute(
            select(Product.category)
            .where(Product.status == "active", Product.category.is_not(None))
            .distinct()
            .order_by(Product.category)
        )
    ).scalars().all()
    return rows


@router.get("/{product_id}", response_model=ProductOut, operation_id="get_product")
async def get_product(product_id: int, db: AsyncSession = Depends(get_db)):
    product = await db.get(Product, product_id)
    if product is None or product.status == "retired":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    out = ProductOut.model_validate(product)
    out.from_price = (await _from_prices(db, [out.model_dump()]))[product.id]
    return out


@router.post(
    "",
    response_model=ProductOut,
    operation_id="create_product",
    dependencies=[Depends(require_role("admin"))],
)
async def create_product(data: ProductCreate, db: AsyncSession = Depends(get_db)):
    product = Product(**data.model_dump())
    db.add(product)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Product SKU already exists") from None
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
    product = await db.get(Product, product_id)
    if product is None or product.status != "active":  # retired products can't be rated
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    verified = await review_svc.is_verified_purchase(db, user.id, product_id)
    # Only a verified purchase publishes immediately; anyone else's review is held for
    # moderation (it was auto-approved, letting any account drive a product's rating).
    initial_status = "approved" if verified else "pending"
    # One rating per user per product — upsert.
    existing = await db.scalar(
        select(Rating).where(Rating.user_id == user.id, Rating.product_id == product_id)
    )
    if existing is not None:
        existing.stars = data.stars
        existing.comment = data.comment
        existing.verified_purchase = verified
        # Editing a review an admin REJECTED must not quietly keep the old verdict on new
        # text — send it back to moderation (hidden from public) instead of auto-publishing.
        if existing.status == "rejected" or (not verified and existing.status == "approved"):
            existing.status = "pending"
        row = existing
        await db.flush()
    else:
        row = Rating(
            user_id=user.id, product_id=product_id, stars=data.stars, comment=data.comment,
            verified_purchase=verified, status=initial_status,
        )
        try:
            # Savepoint: a full rollback would expire `user` (same session) and the next
            # attribute access would 500.
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            # Concurrent first rating by the same user (uq_rating_user_product) — fall back
            # to updating the row the other request committed, with the same rules.
            row = (
                await db.execute(
                    select(Rating)
                    .where(Rating.user_id == user.id, Rating.product_id == product_id)
                    .with_for_update()
                )
            ).scalar_one()
            row.stars = data.stars
            row.comment = data.comment
            row.verified_purchase = verified
            if row.status == "rejected" or (not verified and row.status == "approved"):
                row.status = "pending"
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
    # Only published reviews can be voted on (a pending/rejected one isn't visible).
    if row is None or row.product_id != product_id or row.status != "approved":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
    if row.user_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't vote on your own review")
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
    is_admin = user is not None and user.role == "admin"

    def _out(r: Rating) -> dict:
        o = RatingOut.model_validate(r)
        mine = user is not None and r.user_id == user.id
        o.mine = mine
        # Hide other reviewers' user_id from everyone but admins.
        if not (mine or is_admin):
            o.user_id = None
        return o.model_dump()

    return {
        "product_id": product_id,
        "count": total,
        "average": average,
        "distribution": distribution,
        "ratings": [_out(r) for r in rows],
    }


# ---------- Variants (catalog/display only; checkout stays product-level) ----------
class VariantIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    pack_size: float = Field(default=1, gt=0, le=1_000_000, allow_inf_nan=False)
    price: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)
    mrp: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    image_url: str | None = Field(default=None, max_length=512)
    sort_order: int = Field(default=0, ge=-1_000_000, le=1_000_000)


class VariantUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    pack_size: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    price: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    mrp: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    image_url: str | None = Field(default=None, max_length=512)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")
    sort_order: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)

    _v_nulls = reject_nulls("label", "pack_size", "price", "status", "sort_order")


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    sku: str
    label: str
    pack_size: float
    # Legacy catalogue field — NOT what checkout charges (may be stale). Storefronts must
    # show `selling_price`.
    price: float
    mrp: float | None = None
    image_url: str | None = None
    status: str
    sort_order: int
    # B2C storefront price of this pack: RETAIL unit price x pack_size, GST-inclusive —
    # exactly the cart line total for qty = pack_size. None => price unavailable (no
    # RETAIL row applies at this pack size). Filled on public catalog reads.
    selling_price: float | None = None
    price_available: bool | None = None
    # True when `mrp` should be shown struck through (mrp > selling_price).
    show_mrp_strike: bool = False


async def _priced(db: AsyncSession, variants) -> list[VariantOut]:
    """VariantOut rows with the storefront price from the cart's RETAIL resolver."""
    prices = await pricing.retail_pack_prices(
        db, [(v.product_id, float(v.pack_size)) for v in variants]
    )
    out = []
    for v in variants:
        o = VariantOut.model_validate(v)
        o.selling_price = prices.get((v.product_id, float(v.pack_size)))
        o.price_available = o.selling_price is not None
        o.show_mrp_strike = (
            o.selling_price is not None and o.mrp is not None and o.mrp > o.selling_price
        )
        out.append(o)
    return out


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
    is_admin = user is not None and user.role == "admin"
    product = await db.get(Product, product_id)
    if product is None or (product.status != "active" and not is_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    q = select(ProductVariant).where(ProductVariant.product_id == product_id)
    # Only admins may request inactive variants via ?all=true; everyone else sees active.
    if not (all and user is not None and user.role == "admin"):
        q = q.where(ProductVariant.status == "active")
    q = q.order_by(ProductVariant.sort_order, ProductVariant.id).limit(200)
    return await _priced(db, (await db.execute(q)).scalars().all())


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
    out.variants = await _priced(db, variants)
    out.from_price = await _from_price_one(db, product, out.variants)
    return out


@router.get(
    "/variants/{variant_id}",
    response_model=VariantOut,
    operation_id="get_variant",
    dependencies=[Depends(require_role("admin"))],
)
async def get_variant(variant_id: int, db: AsyncSession = Depends(get_db)):
    """One variant by id, whatever its status (admin edit screens)."""
    variant = await db.get(ProductVariant, variant_id)
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Variant not found")
    return variant


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
