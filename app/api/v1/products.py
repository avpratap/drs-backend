from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_delete, cache_get_json, cache_set_json
from app.core.db import get_db
from app.deps.auth import require_role
from app.models.product import Product
from app.schemas.product import ProductCreate, ProductOut

router = APIRouter(prefix="/products", tags=["products"])

_CACHE_KEY = "cache:products:active"


@router.get("", response_model=list[ProductOut], operation_id="list_products")
async def list_products(db: AsyncSession = Depends(get_db)):
    cached = await cache_get_json(_CACHE_KEY)
    if cached is not None:
        return cached
    result = await db.execute(select(Product).where(Product.status == "active"))
    out = [ProductOut.model_validate(p).model_dump() for p in result.scalars().all()]
    await cache_set_json(_CACHE_KEY, out)
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
    await db.commit()
    await db.refresh(product)
    await cache_delete(_CACHE_KEY)  # invalidate the active-products list
    return product
