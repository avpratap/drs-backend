"""ML endpoints: churn (admin), route optimization (admin), recommendations (public/customer)."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from geoalchemy2 import Geometry
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, paged, pagination
from app.models.churn import ChurnScore
from app.models.delivery import DeliveryOrder
from app.models.product import Product
from app.models.shop import CustomerAccount, DeliveryAssignment, Shop
from app.models.user import User
from app.services.ml import churn as churn_svc
from app.services.ml import recommendations as rec_svc
from app.services.ml import routing as routing_svc

router = APIRouter(tags=["ml"])


# ---------- Schemas ----------
class ChurnScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    account_id: int
    score: float
    risk: str


class ChurnRunOut(BaseModel):
    scored: int
    low: int
    medium: int
    high: int


class RouteStopOut(BaseModel):
    id: int
    seq: int


class RouteOut(BaseModel):
    date: dt.date
    stops: list[RouteStopOut]
    total_distance_km: float


class RecommendationOut(BaseModel):
    product_id: int
    name: str
    sku: str
    image_url: str | None = None
    score: int


def _rec_rows(rows: list[dict]) -> list[RecommendationOut]:
    out = []
    for r in rows:
        score = r.get("bought_together") or r.get("times_bought") or 0
        out.append(RecommendationOut(
            product_id=r["product_id"], name=r["name"], sku=r["sku"],
            image_url=r.get("image_url"), score=int(score),
        ))
    return out


# ---------- Churn (admin) ----------
@router.get("/admin/ml/churn", response_model=list[ChurnScoreOut], operation_id="list_churn_scores",
            dependencies=[Depends(require_role("admin"))])
async def list_churn(
    risk: str | None = None,
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    q = select(ChurnScore).order_by(ChurnScore.score.desc())
    if risk in ("low", "medium", "high"):
        q = q.where(ChurnScore.risk == risk)
    return await paged(db, q, page)


@router.post("/admin/ml/churn/score", response_model=ChurnRunOut, operation_id="run_churn_scoring",
             dependencies=[Depends(require_role("admin"))])
async def run_churn(db: AsyncSession = Depends(get_db)):
    return await churn_svc.score_all(db)


# ---------- Route optimization (admin) ----------
@router.get("/admin/ml/route/optimize", response_model=RouteOut, operation_id="optimize_delivery_route",
            dependencies=[Depends(require_role("admin"))])
async def optimize_route(
    date: dt.date, delivery_id: int | None = None, db: AsyncSession = Depends(get_db)
):
    """Order a date's delivery stops into a short route using shop geo (stops without
    coordinates keep their input order). Pass delivery_id to route one driver's stops
    (their assigned shops); a single call is capped at routing.MAX_STOPS stops."""
    lat = func.ST_Y(func.cast(Shop.geo, Geometry))
    lng = func.ST_X(func.cast(Shop.geo, Geometry))
    rows = (
        await db.execute(
            select(DeliveryOrder.id, lat, lng)
            .join(Shop, Shop.id == DeliveryOrder.shop_id)
            .where(
                DeliveryOrder.delivery_date == date,
                # Only stops still to be driven count toward (and get) a route.
                DeliveryOrder.status.in_(("generated", "frozen")),
                DeliveryOrder.shop_id.in_(
                    select(DeliveryAssignment.shop_id).where(
                        DeliveryAssignment.delivery_id == delivery_id,
                        DeliveryAssignment.active.is_(True),
                    )
                )
                if delivery_id is not None
                else true(),
            )
            .order_by(DeliveryOrder.id)
            .limit(routing_svc.MAX_STOPS + 1)
        )
    ).all()
    points = [{"id": oid, "lat": la, "lng": ln} for oid, la, ln in rows]
    try:
        result = await routing_svc.optimize_route(points)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return RouteOut(
        date=date,
        stops=[RouteStopOut(**s) for s in result["ordered"]],
        total_distance_km=result["total_distance_km"],
    )


# ---------- Recommendations ----------
@router.get("/products/{product_id}/recommendations", response_model=list[RecommendationOut],
            operation_id="product_recommendations")
async def product_recommendations(
    product_id: int, limit: int = Query(default=5, ge=1, le=20), db: AsyncSession = Depends(get_db)
):
    if await db.get(Product, product_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return _rec_rows(await rec_svc.co_purchase(db, product_id, limit))


@router.get("/me/recommendations", response_model=list[RecommendationOut],
            operation_id="my_recommendations")
async def my_recommendations(
    limit: int = Query(default=5, ge=1, le=20),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = (
        await db.execute(select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id))
    ).scalars().all()
    if not account_ids:
        return []
    return _rec_rows(await rec_svc.personalized(db, account_ids[0], limit))
