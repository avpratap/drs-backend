"""Delivery execution at a stop: partial delivery, POD, returns, cash."""
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.fulfillment import CashCollection, ProofOfDelivery, Return
from app.models.inventory import StockMovement
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import batches, inventory
from app.services import notifications as notify
from app.services.billing import bill_delivery


async def mark_delivered(db: AsyncSession, order: DeliveryOrder, user: User, data) -> None:
    # Only an open order may be delivered; refuse to clobber a terminal one so a
    # second (concurrent or stale) submission can't overwrite the first.
    if order.status not in ("generated", "frozen"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Order is {order.status}; only generated/frozen orders can be delivered",
        )
    res = await db.execute(
        select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
    )
    items = {i.product_id: i for i in res.scalars().all()}

    # The submission must account for EVERY planned line (qty_delivered=0 means
    # not-delivered). Otherwise an omitted item would stay stuck 'planned' and billed
    # ₹0 with no way to correct it (the order is terminal after this call).
    submitted = {di.product_id for di in data.items}
    if len(submitted) != len(data.items):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Duplicate product_id in delivery items; report each product once",
        )
    missing = set(items.keys()) - submitted
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Delivery must report every planned item (use qty_delivered=0 for not "
            f"delivered); missing product(s): {sorted(missing)}",
        )

    for di in data.items:
        it = items.get(di.product_id)
        if it is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Product {di.product_id} is not in this order",
            )
        if di.qty_delivered < 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"qty_delivered must be >= 0 for product {di.product_id}",
            )
        # Cap at the planned (consented) quantity: any increase must go through a
        # pre-cutoff subscription override, so delivering more can't over-bill.
        if di.qty_delivered > float(it.qty_planned):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"qty_delivered ({di.qty_delivered}) exceeds planned ({float(it.qty_planned)}) "
                f"for product {di.product_id}",
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

    # Aggregate + validate returns PER product: a return must be of a product on the
    # order and can't exceed what was delivered for it. Otherwise returning product B
    # could cancel the charge for delivered product A, or returning more than delivered
    # would synthesize phantom stock.
    returned_qty: dict[int, float] = {}
    # Restockable returns (goods physically come back to the van/warehouse) vs destroyed
    # (damaged/expired — they left stock and don't return). Only restockable units are
    # excluded from the stock depletion below; destroyed ones are written off.
    _RESTOCKABLE = {"refused", "excess", "wrong_item"}
    restockable_qty: dict[int, float] = {}
    for r in data.returns:
        if r.product_id not in items:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Return product {r.product_id} is not in this order",
            )
        returned_qty[r.product_id] = returned_qty.get(r.product_id, 0.0) + float(r.qty_returned)
        if r.reason in _RESTOCKABLE:
            restockable_qty[r.product_id] = restockable_qty.get(r.product_id, 0.0) + float(r.qty_returned)
    for pid, rq in returned_qty.items():
        if rq > float(items[pid].qty_delivered):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Returned qty ({rq}) exceeds delivered for product {pid}",
            )

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

    # Net chargeable = per-line (delivered - returned) value, floored at 0 per product
    # so a return can never reduce the charge for a *different* delivered product.
    # bill_delivery is idempotent per delivery_order, so a corrected re-submit never
    # double-charges.
    # Round PER LINE exactly like the GST invoice does (charged = round(price * net qty, 2)
    # per line) — rounding the sum once drifted a paisa from the invoice total.
    net = round(
        sum(
            round(
                max(
                    float(it.unit_price)
                    * round(float(it.qty_delivered) - returned_qty.get(pid, 0.0), 3),
                    0.0,
                ),
                2,
            )
            for pid, it in items.items()
        ),
        2,
    )
    # Update the order/line totals to the ACTUAL delivered amounts (they held planned
    # values from cutoff). Otherwise amount_due would mislead a COD driver on a partial.
    for it in items.values():
        it.line_total = round(float(it.unit_price) * float(it.qty_delivered), 2)
    order.subtotal = round(sum(float(it.line_total) for it in items.values()), 2)
    order.total = net
    order.amount_due = net
    await bill_delivery(db, order, net)

    # Decrement stock for what actually went out (delivered - returned), once per
    # delivery_order so a corrected re-submit never double-depletes inventory.
    already_stocked = await db.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.ref_type == "delivery_order", StockMovement.ref_id == order.id)
    )
    if not already_stocked:
        loc = await inventory.default_location_id(db)
        # returned_qty is validated above as <= qty_delivered per product, so out_qty
        # is always >= 0 — we only ever deplete what the customer kept; returned units
        # were never dispatched out of stock, so nothing is added back.
        for pid, it in items.items():
            # Deplete what physically left stock: delivered minus the units that came back
            # restockable. Damaged/expired returns are NOT added back — they're written off.
            out_qty = round(float(it.qty_delivered) - restockable_qty.get(pid, 0.0), 3)
            if out_qty > 0:  # FEFO: deplete earliest-expiry batches first
                await batches.consume_fefo(
                    db, pid, out_qty, "delivery", location_id=loc,
                    ref_type="delivery_order", ref_id=order.id, created_by=user.id,
                )

    # Notify the account owner that their stop is done (idempotent per order).
    account = await db.get(CustomerAccount, order.account_id)
    owner = await db.get(User, account.owner_user_id) if account else None
    if owner is not None:
        delivered_count = sum(1 for i in items.values() if i.status in ("delivered", "partial"))
        await notify.emit_event(
            db,
            "delivery.completed",
            owner,
            {"summary": f"{delivered_count} item(s)", "amount": net},
            account_id=order.account_id,
            dedupe_key=f"delivery.completed:{order.id}",
        )

    await db.commit()
