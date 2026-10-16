"""AI endpoints: customer support bot + admin copilot.

DPDP-safe: we send Claude tool-fetched facts (order ids, statuses, totals) and the
user's question — never raw PII dumps. Dev-stubbed until ANTHROPIC_API_KEY is set.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.models.order import Order
from app.models.shop import CustomerAccount
from app.models.subscription import Subscription
from app.models.user import User
from app.services import ai

router = APIRouter(tags=["ai"])

_SUPPORT_SYSTEM = (
    "You are DRS's customer support assistant for a B2B dairy brand (milk, bilona "
    "ghee, buttermilk, cheese). Be concise, friendly, and accurate. Use only the "
    "ACCOUNT FACTS provided; if you lack the info, say you'll escalate to a human. "
    "Never invent order numbers or amounts."
)
_COPILOT_SYSTEM = (
    "You are DRS's admin operations copilot. Answer the admin's question using only "
    "the METRICS provided. Be precise and brief; if the metrics don't cover it, say so."
)


class AskIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class AiAnswerOut(BaseModel):
    answer: str
    ai_live: bool


@router.post("/ai/support", operation_id="ai_support", response_model=AiAnswerOut)
async def ai_support(
    data: AskIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Customer support bot grounded in the caller's own recent orders."""
    await ai.check_and_increment(user.id, scope="support")
    account_ids = set(
        (
            await db.execute(
                select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id)
            )
        ).scalars().all()
    )
    facts = ["ACCOUNT FACTS:"]
    if account_ids:
        orders = (
            await db.execute(
                select(Order)
                .where(Order.account_id.in_(account_ids))
                .order_by(Order.id.desc())
                .limit(5)
            )
        ).scalars().all()
        for o in orders:
            facts.append(
                f"- order {o.order_no}: status={o.status}, payment={o.payment_status}, "
                f"total=₹{float(o.total):.2f}"
            )
        if len(facts) == 1:
            facts.append("- no recent orders on file")
    else:
        facts.append("- no account/orders yet")
    prompt = f"{chr(10).join(facts)}\n\nCUSTOMER QUESTION: {data.message}"
    answer = await ai.complete(_SUPPORT_SYSTEM, prompt)
    return {"answer": answer, "ai_live": ai.ai_enabled()}


@router.post(
    "/admin/ai/ask",
    operation_id="admin_ai_copilot",
    response_model=AiAnswerOut,
    dependencies=[Depends(require_role("admin"))],
)
async def admin_ai_copilot(
    data: AskIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin copilot grounded in live aggregate metrics (sanitized, no PII)."""
    await ai.check_and_increment(user.id, scope="copilot")
    paid_revenue = await db.scalar(
        select(func.coalesce(func.sum(Order.total), 0)).where(Order.payment_status == "paid")
    )
    active_subs = await db.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.status == "active")
    )
    orders_count = await db.scalar(select(func.count()).select_from(Order))
    metrics = (
        "METRICS:\n"
        f"- paid order revenue: ₹{float(paid_revenue or 0):.2f}\n"
        f"- active subscriptions: {active_subs or 0}\n"
        f"- total orders: {orders_count or 0}"
    )
    prompt = f"{metrics}\n\nADMIN QUESTION: {data.message}"
    answer = await ai.complete(_COPILOT_SYSTEM, prompt)
    return {"answer": answer, "ai_live": ai.ai_enabled()}
