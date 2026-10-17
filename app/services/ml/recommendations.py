"""Product recommendations.

- co_purchase: "customers who bought X also bought Y" from OrderItem co-occurrence.
- personalized: reorder suggestions from an account's own past purchases, ranked by
  frequency then recency.

Pure SQL aggregation (no heavy libs); an `implicit` ALS model can later replace
co_purchase behind the same signature. On-demand, no cached table.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import Product


async def co_purchase(db: AsyncSession, product_id: int, limit: int = 5) -> list[dict]:
    """Products most often bought in the same orders as `product_id`."""
    oi2 = OrderItem.__table__.alias("oi2")
    rows = (
        await db.execute(
            select(oi2.c.product_id, func.count().label("n"))
            .select_from(OrderItem)
            .join(oi2, oi2.c.order_id == OrderItem.order_id)
            # Filter inactive products BEFORE the LIMIT so we never return fewer than asked.
            .join(Product, Product.id == oi2.c.product_id)
            .where(
                OrderItem.product_id == product_id,
                oi2.c.product_id != product_id,
                Product.status == "active",
            )
            .group_by(oi2.c.product_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    return await _hydrate(db, [(pid, int(n)) for pid, n in rows], score_key="bought_together")


async def personalized(db: AsyncSession, account_id: int, limit: int = 5) -> list[dict]:
    """Reorder suggestions from the account's own history (frequency, then recency)."""
    rows = (
        await db.execute(
            select(
                OrderItem.product_id,
                func.count().label("n"),
                func.max(Order.created_at).label("last"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(Order.account_id == account_id, Product.status == "active")
            .group_by(OrderItem.product_id)
            .order_by(func.count().desc(), func.max(Order.created_at).desc())
            .limit(limit)
        )
    ).all()
    return await _hydrate(db, [(pid, int(n)) for pid, n, _last in rows], score_key="times_bought")


async def _hydrate(db: AsyncSession, scored: list[tuple[int, int]], score_key: str) -> list[dict]:
    if not scored:
        return []
    products = {
        p.id: p
        for p in (
            await db.execute(
                select(Product).where(Product.id.in_([pid for pid, _ in scored]))
            )
        ).scalars().all()
    }
    out = []
    for pid, n in scored:
        p = products.get(pid)
        if p is None or p.status != "active":
            continue
        out.append({
            "product_id": pid,
            "name": p.name,
            "sku": p.sku,
            "image_url": p.image_url,
            score_key: n,
        })
    return out
