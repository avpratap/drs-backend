"""Delivery execution at a stop: partial delivery, POD, returns, cash."""
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import media
from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.fulfillment import CashCollection, ProofOfDelivery, Return
from app.models.inventory import StockMovement
from app.models.shop import CustomerAccount
from app.models.user import User
from app.services import batches, inventory
from app.services import notifications as notify
from app.services.billing import (
    InsufficientPrepaidBalance,
    bill_delivery,
    credit_entry,
    wallet_adjust,
)
from app.services.subscription import today_ist


async def mark_delivered(db: AsyncSession, order: DeliveryOrder, user: User, data) -> None:
    # Only an open order may be delivered; refuse to clobber a terminal one so a
    # second (concurrent or stale) submission can't overwrite the first.
    if order.status not in ("generated", "frozen"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Order is {order.status}; only generated/frozen orders can be delivered",
        )
    if user.role != "admin" and order.delivery_date > today_ist():
        # A future stop delivered (and billed) now would survive a later skip/cutoff.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This stop is scheduled for a later date"
        )
    delivered_any = any(float(di.qty_delivered) > 0 for di in data.items)
    if (
        user.role != "admin"
        and delivered_any
        and (data.pod is None or not (data.pod.photo_url or data.pod.signature_url))
    ):
        # Proof of delivery is what settles a "never received it" dispute: a driver
        # must attach a photo or signature (admins correcting records are exempt).
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Proof of delivery (photo or signature) is required",
        )
    if data.pod is not None:
        for ref in (data.pod.photo_url, data.pod.signature_url):
            if media.is_ref(ref) and user.role != "admin" and not media.ref_owned(ref, "pod", user.id):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown POD upload")
    res = await db.execute(
        select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
    )
    # Ordered by product_id: the stock loop below locks stock items in this order (the
    # same global order as transfers/production/GRN), so two deliveries can't deadlock.
    items = {i.product_id: i for i in sorted(res.scalars().all(), key=lambda i: i.product_id)}

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

    # Net chargeable = per-line (delivered - returned) value, floored at 0 per product
    # so a return can never reduce the charge for a *different* delivered product.
    # bill_delivery is idempotent per delivery_order, so a corrected re-submit never
    # double-charges.
    # Round PER LINE exactly like the GST invoice does (charged = round(price * net qty, 2)
    # per line) — rounding the sum once drifted a paisa from the invoice total.
    # The lines and subtotal now carry these NET values too (they held the planned values
    # from cutoff, and were later set to GROSS delivered, ignoring door returns, so a COD
    # driver's line/subtotal figures disagreed with total/amount_due and the invoice).
    for pid, it in items.items():
        it.line_total = round(
            max(
                float(it.unit_price)
                * round(float(it.qty_delivered) - returned_qty.get(pid, 0.0), 3),
                0.0,
            ),
            2,
        )
    net = round(sum(float(it.line_total) for it in items.values()), 2)
    order.subtotal = net
    order.total = net
    order.amount_due = net
    # Cash/UPI taken at the door is posted BEFORE billing: for a prepaid account it is
    # what lets a short wallet still cover this delivery.
    if data.cash_collected:
        # Cash/UPI taken at the door PAYS this delivery: post it against the bill (else a
        # COD customer was charged again on their wallet/credit) — and never more than
        # what is due (a typo'd ₹1,000,000 on a ₹60 stop would inflate the books).
        cash = round(float(data.cash_collected), 2)
        if cash > net:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Cash collected ({cash}) exceeds the amount due ({net})",
            )
        cc = CashCollection(
            delivery_order_id=order.id,
            amount_collected=cash,
            method=data.cash_method or "cash",
            reference=data.cash_reference,
            collected_by=user.id,
        )
        db.add(cc)
        await db.flush()
        account = await db.get(CustomerAccount, order.account_id)
        if account is not None and account.billing_mode == "postpaid_credit":
            await credit_entry(db, account.id, -cash, "payment", "cash_collection", cc.id)
        else:
            await wallet_adjust(db, order.account_id, cash, "cash_collection", "cash_collection", cc.id)

    try:
        await bill_delivery(db, order, net)
    except InsufficientPrepaidBalance as exc:
        # Prepaid invariant: never deliver (bill) into the negative. Defer the stop —
        # roll everything back (nothing billed, no POD/stock/cash recorded, the order
        # stays open), nudge the owner to top up, and tell the driver.
        order_id, account_id = order.id, order.account_id
        await db.rollback()
        account = await db.get(CustomerAccount, account_id)
        owner = await db.get(User, account.owner_user_id) if account else None
        if owner is not None:
            await notify.emit_event(
                db, "wallet.low", owner,
                {"balance": round(exc.balance, 2), "needed": exc.needed, "delivery_id": order_id},
                account_id=account_id, dedupe_key=f"delivery.lowbalance:{order_id}",
            )
            await db.commit()
        short = round(exc.needed - exc.balance, 2)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Prepaid wallet can't cover this delivery (Rs {exc.needed:.2f} due, short by "
            f"Rs {short:.2f}). Collect the shortfall as cash (cash_collected) or skip "
            "this stop; nothing was billed.",
        ) from None

    # Decrement stock for what actually went out (delivered - returned), once per
    # delivery_order so a corrected re-submit never double-depletes inventory.
    already_stocked = await db.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.ref_type == "delivery_order", StockMovement.ref_id == order.id)
    )
    if not already_stocked:
        loc = await inventory.delivery_location_id(
            db, order.shop_id, user.id, actor_is_admin=user.role == "admin"
        )
        # returned_qty is validated above as <= qty_delivered per product, so out_qty
        # is always >= 0 — we only ever deplete what the customer kept; returned units
        # were never dispatched out of stock, so nothing is added back.
        for pid, it in items.items():
            # Deplete what physically left stock: delivered minus the units that came back
            # restockable. Damaged/expired returns are NOT added back — they're written off.
            out_qty = round(float(it.qty_delivered) - restockable_qty.get(pid, 0.0), 3)
            if out_qty > 0:  # FEFO: deplete earliest-expiry batches first
                try:
                    await batches.consume_fefo(
                        db, pid, out_qty, "delivery", location_id=loc,
                        ref_type="delivery_order", ref_id=order.id, created_by=user.id,
                    )
                except batches.InsufficientStock as exc:
                    # ALLOW_NEGATIVE_STOCK is off: refuse rather than backorder. Nothing
                    # of this delivery (billing, POD, stock) is committed.
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        f"Not enough stock for product {pid} at the delivering location: "
                        f"{exc.available:g} on hand, {out_qty:g} needed. Receive or "
                        "transfer stock first.",
                    ) from None

    # Notify the account owner that their stop is done (idempotent per order).
    account = await db.get(CustomerAccount, order.account_id)
    owner = await db.get(User, account.owner_user_id) if account else None
    if owner is not None:
        delivered_count = sum(1 for i in items.values() if i.status in ("delivered", "partial"))
        await notify.emit_event(
            db,
            "delivery.completed",
            owner,
            {"summary": f"{delivered_count} item(s)", "amount": net, "delivery_id": order.id},
            account_id=order.account_id,
            dedupe_key=f"delivery.completed:{order.id}",
        )

    await db.commit()
