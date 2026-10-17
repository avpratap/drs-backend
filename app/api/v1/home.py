"""Customer home-screen aggregate — one call instead of 6+ round-trips.

Resilient: a failure in any optional section (loyalty, banners, recommendations)
degrades to a default rather than 500ing the whole screen."""
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.models.loyalty import LoyaltyAccount
from app.models.order import Order
from app.models.subscription import Subscription
from app.models.user import User
from app.services import billing as billing_svc
from app.services import content as content_svc
from app.services import orders as order_svc
from app.services.ml import recommendations as rec_svc

router = APIRouter(tags=["home"])
log = logging.getLogger("drs.home")


class RecentOrderOut(BaseModel):
    id: int
    order_no: str
    status: str
    total: float


class HomeBannerOut(BaseModel):
    id: int
    title: str
    image_url: str
    cta_link: str | None = None


class HomeOut(BaseModel):
    wallet_balance: float
    credit_outstanding: float
    loyalty_points: int
    active_subscriptions: int
    recent_orders: list[RecentOrderOut]
    banners: list[HomeBannerOut]
    recommendations: list[dict]


@router.get("/me/home", response_model=HomeOut, operation_id="my_home")
async def my_home(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    account = await order_svc._get_or_create_account(db, user)

    wallet = await billing_svc.get_or_create_wallet(db, account.id)
    credit = await billing_svc.credit_outstanding(db, account.id)

    active_subs = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.account_id == account.id, Subscription.status == "active")
    )

    orders = (
        await db.execute(
            select(Order)
            .where(Order.account_id == account.id)
            .order_by(Order.id.desc())
            .limit(5)
        )
    ).scalars().all()
    recent = [
        RecentOrderOut(id=o.id, order_no=o.order_no, status=o.status, total=float(o.total))
        for o in orders
    ]

    # Optional sections — best-effort, never fail the whole screen.
    loyalty_points = 0
    try:
        la = await db.scalar(
            select(LoyaltyAccount).where(LoyaltyAccount.account_id == account.id)
        )
        loyalty_points = int(la.points_balance) if la else 0
    except Exception:
        log.warning("home: loyalty lookup failed")

    banners: list[HomeBannerOut] = []
    try:
        banners = [
            HomeBannerOut(id=b.id, title=b.title, image_url=b.image_url, cta_link=b.cta_link)
            for b in await content_svc.active_banners(db)
        ]
    except Exception:
        log.warning("home: banners lookup failed")

    recommendations: list[dict] = []
    try:
        recommendations = await rec_svc.personalized(db, account.id, limit=5)
    except Exception:
        log.warning("home: recommendations failed")

    return HomeOut(
        wallet_balance=float(wallet.balance),
        credit_outstanding=float(credit),
        loyalty_points=loyalty_points,
        active_subscriptions=int(active_subs or 0),
        recent_orders=recent,
        banners=banners,
        recommendations=recommendations,
    )
