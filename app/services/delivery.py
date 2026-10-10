"""Delivery execution at a stop: partial delivery, POD, returns, cash."""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.fulfillment import CashCollection, ProofOfDelivery, Return
from app.models.user import User
from app.services.billing import bill_delivery


async def mark_delivered(db: AsyncSession, order: DeliveryOrder, user: User, data) -> None:
    res = await db.execute(
        select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
    )
    items = {i.product_id: i for i in res.scalars().all()}

    for di in data.items:
        it = items.get(di.product_id)
        if it is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Product {di.product_id} is not in this order",
            )
        it.qty_delivered = di.qty_delivered
        planned = float(it.qty_planned)
        if di.qty_delivered <= 0:
            it.status = "not_delivered"
        elif di.qty_delivered < planned:
            it.status = "partial"
        else:
            it.status = "delivered"

    statuses = [i.status for i in items.values()]
    if statuses and all(s == "delivered" for s in statuses):
        order.status = "delivered"
    elif statuses and all(s == "not_delivered" for s in statuses):
        order.status = "failed"
    else:
        order.status = "partially_delivered"

    if data.pod is not None:
        db.add(
            ProofOfDelivery(
                delivery_order_id=order.id,
                delivered_by=user.id,
                recipient_name=data.pod.recipient_name,
                photo_url=data.pod.photo_url,
                signature_url=data.pod.signature_url,
                lat=data.pod.lat,
                lng=data.pod.lng,
                note=data.pod.note,
            )
        )

    for r in data.returns:
        db.add(
            Return(
                delivery_order_id=order.id,
                product_id=r.product_id,
                qty_returned=r.qty_returned,
                reason=r.reason,
                created_by=user.id,
            )
        )

    if data.cash_collected is not None:
        db.add(
            CashCollection(
                delivery_order_id=order.id,
                amount_collected=data.cash_collected,
                method=data.cash_method or "cash",
                reference=data.cash_reference,
                collected_by=user.id,
            )
        )

    # Net chargeable = delivered value − returned value. bill_delivery is idempotent
    # per delivery_order, so a corrected re-submit never double-charges.
    delivered_total = sum(float(i.unit_price) * float(i.qty_delivered) for i in items.values())
    returns_total = sum(
        float(items[r.product_id].unit_price) * float(r.qty_returned)
        for r in data.returns
        if r.product_id in items
    )
    net = round(delivered_total - returns_total, 2)
    await bill_delivery(db, order, net)

    await db.commit()
