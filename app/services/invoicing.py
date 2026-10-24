"""GST invoice + credit-note generation.

- Financial year = Apr 1 - Mar 31; invoice numbers are sequential per FY, serialized
  with an advisory lock so two concurrent generations can't collide.
- Place of supply (buyer state) vs seller state decides CGST+SGST vs IGST.
- One tax invoice per delivery order (idempotent); credit notes mirror an invoice
  with negated amounts for returns/refunds.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.gst_states import canonical, same_state
from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.fulfillment import Return
from app.models.invoice import Invoice, InvoiceLine
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.profile import Address, UserProfile
from app.models.shop import CustomerAccount, Shop


class NothingToInvoice(ValueError):
    """The document would have no lines (nothing supplied)."""


def _today() -> dt.date:
    """Invoice/credit-note date in IST (not UTC) so the FY serial and invoice_date are
    correct at the Apr-1 boundary and during the 00:00-05:30 IST window."""
    return dt.datetime.now(ZoneInfo(settings.TIMEZONE)).date()


_SEQ_LOCK_NS = 555001
_INV_LOCK_NS = 555002  # serialize invoice generation per delivery order
_CN_LOCK_NS = 555003  # serialize credit-note generation per source invoice
_ORD_INV_LOCK_NS = 555004  # serialize invoice generation per one-time order


def _lk(n: int) -> int:
    """Mask a 64-bit id into the signed int4 range the 2-arg advisory-lock form needs
    (ids beyond 2^31 would otherwise raise). Collisions are astronomically unlikely."""
    return n & 0x7FFFFFFF


def fy_for_date(d: dt.date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


async def _next_seq(db: AsyncSession, fy: str, is_credit_note: bool = False) -> int:
    # Tax invoices and credit notes keep SEPARATE consecutive series (GST requires a
    # gap-free serial per document type). Distinct lock keys so the two don't block
    # each other, and the max() is filtered by document type.
    key = int(fy[:4]) * 2 + (1 if is_credit_note else 0)
    await db.execute(select(func.pg_advisory_xact_lock(_SEQ_LOCK_NS, key)))
    last = await db.scalar(
        select(func.max(Invoice.seq_no)).where(
            Invoice.fy == fy, Invoice.is_credit_note.is_(is_credit_note)
        )
    )
    return (last or 0) + 1


async def _place_of_supply(db: AsyncSession, order: DeliveryOrder) -> str:
    """Place of supply for a delivery: the shop's own state (most accurate), else the
    account's default-address state, else the seller state (intra-state) as last resort."""
    shop = await db.get(Shop, order.shop_id)
    if shop is not None and shop.state:
        return shop.state
    state = await db.scalar(
        select(Address.state)
        .where(
            Address.account_id == order.account_id,
            Address.status == "active",
            Address.is_default.is_(True),
        )
        .limit(1)
    )
    return state or settings.SELLER_STATE


async def _buyer_gstin(db: AsyncSession, account_id: int) -> str | None:
    account = await db.get(CustomerAccount, account_id)
    if account is None:
        return None
    prof = await db.get(UserProfile, account.owner_user_id)
    return prof.gstin if prof else None


def _inclusive_split(
    charged: float, rate: float, interstate: bool
) -> tuple[float, float, float, float]:
    """Prices are GST-INCLUSIVE (MRP): the `charged` amount is what the customer paid.
    Back out the taxable value and tax so taxable + all taxes == charged EXACTLY, and
    the invoice line total equals the amount collected.

    Returns (taxable_value, cgst, sgst, igst)."""
    taxable = round(charged / (1 + rate / 100), 2)
    total_tax = round(charged - taxable, 2)
    if interstate:
        return taxable, 0.0, 0.0, total_tax
    # Halve from the single rounded total so CGST+SGST always equals it (no paisa drift).
    cgst = round(total_tax / 2, 2)
    return taxable, cgst, round(total_tax - cgst, 2), 0.0


async def generate_for_delivery(db: AsyncSession, order: DeliveryOrder) -> Invoice:
    # Serialize concurrent generations for this order so the existence check below
    # is reliable (partial-unique index on delivery_order_id is the DB backstop).
    await db.execute(select(func.pg_advisory_xact_lock(_INV_LOCK_NS, _lk(order.id))))
    # Idempotent: reuse the existing tax invoice for this delivery order.
    existing = await db.scalar(
        select(Invoice).where(
            Invoice.delivery_order_id == order.id, Invoice.is_credit_note.is_(False)
        )
    )
    if existing is not None:
        return existing

    rows = (
        await db.execute(
            select(DeliveryOrderItem).where(DeliveryOrderItem.delivery_order_id == order.id)
        )
    ).scalars().all()
    # Net out door returns per product so the invoice bills the NET delivered quantity
    # (== what delivery billing charged), not the gross delivered.
    returned_rows = (
        await db.execute(
            select(Return.product_id, func.sum(Return.qty_returned))
            .where(Return.delivery_order_id == order.id)
            .group_by(Return.product_id)
        )
    ).all()
    returned = {pid: float(q or 0) for pid, q in returned_rows}
    chargeable = []
    for it in rows:
        qty = round(float(it.qty_delivered or 0) - returned.get(it.product_id, 0.0), 3)
        if qty > 0:
            chargeable.append((it, qty))
    if not chargeable:
        # Fully returned / nothing delivered: no supply → no tax invoice, and no serial
        # burned on a zero-value document (GST serials must stay gap-free).
        raise NothingToInvoice("nothing was supplied on this delivery order")

    place = canonical(await _place_of_supply(db, order)) or settings.SELLER_STATE
    # Compare on the GST state code, so "RJ" / "rajasthan." / "08" are all intra-state.
    interstate = not same_state(place, settings.SELLER_STATE)
    today = _today()
    fy = fy_for_date(today)
    seq = await _next_seq(db, fy)

    inv = Invoice(
        invoice_no=f"{settings.INVOICE_PREFIX}/{fy}/{seq:06d}",
        fy=fy,
        seq_no=seq,
        account_id=order.account_id,
        delivery_order_id=order.id,
        invoice_date=today,
        seller_gstin=settings.SELLER_GSTIN or None,
        buyer_gstin=await _buyer_gstin(db, order.account_id),
        place_of_supply=place,
        is_interstate=interstate,
    )
    db.add(inv)
    await db.flush()

    tax_val = cgst = sgst = igst = total = 0.0
    for it, qty in chargeable:
        product = await db.get(Product, it.product_id)
        rate = float(product.gst_rate) if product else 0.0
        # unit_price is GST-inclusive (MRP); charged is what delivery billing collected.
        charged = round(float(it.unit_price) * qty, 2)
        taxable, c, s, i = _inclusive_split(charged, rate, interstate)
        line_total = charged
        db.add(
            InvoiceLine(
                invoice_id=inv.id,
                product_id=it.product_id,
                hsn_code=product.hsn_code if product else None,
                description=product.name if product else None,
                qty=qty,
                unit_price=float(it.unit_price),
                taxable_value=taxable,
                gst_rate=rate,
                cgst=c,
                sgst=s,
                igst=i,
                line_total=line_total,
            )
        )
        tax_val += taxable
        cgst += c
        sgst += s
        igst += i
        total += line_total

    inv.taxable_value = round(tax_val, 2)
    inv.cgst = round(cgst, 2)
    inv.sgst = round(sgst, 2)
    inv.igst = round(igst, 2)
    inv.total = round(total, 2)
    await db.commit()
    await db.refresh(inv)
    return inv


async def _order_place_of_supply(db: AsyncSession, order: Order) -> str:
    """For a one-time D2C order, place of supply is where THIS order ships. Prefer the
    order's own captured delivery state (authoritative per-order, so a repeat customer or
    one with a different saved address is still taxed correctly), then the buyer's default
    address state, then the storefront shop's state, then the seller state as a last resort."""
    if order.ship_state:
        return order.ship_state
    state = await db.scalar(
        select(Address.state).where(
            Address.account_id == order.account_id,
            Address.status == "active",
            Address.is_default.is_(True),
        ).limit(1)
    )
    if state:
        return state
    shop = await db.get(Shop, order.shop_id)
    if shop is not None and shop.state:
        return shop.state
    return settings.SELLER_STATE


async def generate_for_order(db: AsyncSession, order: Order) -> Invoice:
    """GST tax invoice for a one-time (D2C storefront) Order. Idempotent per order."""
    await db.execute(select(func.pg_advisory_xact_lock(_ORD_INV_LOCK_NS, _lk(order.id))))
    existing = await db.scalar(
        select(Invoice).where(Invoice.order_id == order.id, Invoice.is_credit_note.is_(False))
    )
    if existing is not None:
        return existing

    place = canonical(await _order_place_of_supply(db, order)) or settings.SELLER_STATE
    interstate = not same_state(place, settings.SELLER_STATE)
    today = _today()
    fy = fy_for_date(today)
    seq = await _next_seq(db, fy)
    inv = Invoice(
        invoice_no=f"{settings.INVOICE_PREFIX}/{fy}/{seq:06d}",
        fy=fy,
        seq_no=seq,
        account_id=order.account_id,
        order_id=order.id,
        invoice_date=today,
        seller_gstin=settings.SELLER_GSTIN or None,
        buyer_gstin=await _buyer_gstin(db, order.account_id),
        place_of_supply=place,
        is_interstate=interstate,
    )
    db.add(inv)
    await db.flush()

    rows = list(
        (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    )
    # Prices are GST-inclusive; order.total (subtotal - discount) is exactly what the
    # customer paid. Apportion that charged total across lines by the discount factor,
    # giving the LAST line the running remainder so the rounded line charges sum to
    # order.total to the paisa (invoice.total == amount charged).
    subtotal = float(order.subtotal) or 0.0
    disc_factor = (float(order.total) / subtotal) if subtotal > 0 else 1.0
    target = round(float(order.total), 2)
    running = 0.0
    n = len(rows)
    tax_val = cgst = sgst = igst = total = 0.0
    for idx, it in enumerate(rows):
        qty = float(it.qty)
        product = await db.get(Product, it.product_id)
        rate = float(product.gst_rate) if product else 0.0
        if idx < n - 1:
            charged = round(float(it.line_total) * disc_factor, 2)
        else:
            charged = round(target - running, 2)  # last line absorbs rounding remainder
        running = round(running + charged, 2)
        taxable, c, s, i = _inclusive_split(charged, rate, interstate)
        line_total = charged
        db.add(
            InvoiceLine(
                invoice_id=inv.id, product_id=it.product_id,
                hsn_code=product.hsn_code if product else None,
                description=product.name if product else None,
                qty=qty, unit_price=float(it.unit_price), taxable_value=taxable,
                gst_rate=rate, cgst=c, sgst=s, igst=i, line_total=line_total,
            )
        )
        tax_val += taxable
        cgst += c
        sgst += s
        igst += i
        total += line_total
    inv.taxable_value = round(tax_val, 2)
    inv.cgst = round(cgst, 2)
    inv.sgst = round(sgst, 2)
    inv.igst = round(igst, 2)
    inv.total = round(total, 2)
    await db.commit()
    await db.refresh(inv)
    return inv


def _new_credit_note(invoice: Invoice, *, partial: bool) -> Invoice:
    today = _today()  # ONE read: fy and invoice_date can't straddle midnight on Mar 31
    fy = fy_for_date(today)
    return Invoice(
        invoice_no="",  # numbered by the caller (needs the serial under its lock)
        fy=fy,
        seq_no=0,
        account_id=invoice.account_id,
        delivery_order_id=invoice.delivery_order_id,
        order_id=invoice.order_id,
        invoice_date=today,
        seller_gstin=invoice.seller_gstin,
        buyer_gstin=invoice.buyer_gstin,
        place_of_supply=invoice.place_of_supply,
        is_interstate=invoice.is_interstate,
        is_credit_note=True,
        is_partial=partial,
        original_invoice_id=invoice.id,
        status="issued",
    )


async def _number_credit_note(db: AsyncSession, note: Invoice) -> None:
    note.seq_no = await _next_seq(db, note.fy, is_credit_note=True)
    note.invoice_no = f"{settings.INVOICE_PREFIX}/CN/{note.fy}/{note.seq_no:06d}"


async def _existing_credit_note(db: AsyncSession, invoice: Invoice) -> Invoice | None:
    # Caller holds the per-invoice _CN_LOCK_NS lock; populate_existing so a CN updated
    # by an earlier transaction in this same session isn't read stale.
    return (
        await db.execute(
            select(Invoice)
            .where(Invoice.original_invoice_id == invoice.id, Invoice.is_credit_note.is_(True))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _retotal(note: Invoice, lines: list[InvoiceLine]) -> None:
    note.taxable_value = round(sum(float(ln.taxable_value) for ln in lines), 2)
    note.cgst = round(sum(float(ln.cgst) for ln in lines), 2)
    note.sgst = round(sum(float(ln.sgst) for ln in lines), 2)
    note.igst = round(sum(float(ln.igst) for ln in lines), 2)
    note.total = round(sum(float(ln.line_total) for ln in lines), 2)
    note.pdf_url = None  # any cached PDF no longer matches — re-render on next request


async def generate_credit_note(db: AsyncSession, invoice: Invoice, reason: str | None) -> Invoice:
    """FULL reversal of an invoice. One credit note per invoice (uq_credit_note_original):
    idempotent if a full CN exists; an existing PARTIAL CN is upgraded in place to the
    full reversal (so the invoice is reversed exactly once). Commits."""
    if invoice.is_credit_note:
        raise ValueError("cannot issue a credit note against a credit note")
    await db.execute(select(func.pg_advisory_xact_lock(_CN_LOCK_NS, _lk(invoice.id))))
    existing = await _existing_credit_note(db, invoice)
    if existing is not None and not existing.is_partial:
        return existing

    orig_lines = (
        await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
    ).scalars().all()
    if existing is not None:
        await db.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id == existing.id))
        note = existing
        note.is_partial = False
    else:
        note = _new_credit_note(invoice, partial=False)
        await _number_credit_note(db, note)
        db.add(note)
        await db.flush()
    new_lines = [
        InvoiceLine(
            invoice_id=note.id,
            product_id=ln.product_id,
            hsn_code=ln.hsn_code,
            description=((reason or "Credit note") + f" — {ln.description or ''}")[:255],
            qty=-float(ln.qty),
            unit_price=float(ln.unit_price),
            taxable_value=round(-float(ln.taxable_value), 2),
            gst_rate=float(ln.gst_rate),
            cgst=round(-float(ln.cgst), 2),
            sgst=round(-float(ln.sgst), 2),
            igst=round(-float(ln.igst), 2),
            line_total=round(-float(ln.line_total), 2),
        )
        for ln in orig_lines
    ]
    db.add_all(new_lines)
    _retotal(note, new_lines)
    await db.commit()
    await db.refresh(note)
    return note


async def generate_credit_note_for_lines(
    db: AsyncSession, invoice: Invoice, returned_qty: dict[int, float] | None, reason: str | None,
    amount: float | None = None,
) -> Invoice | None:
    """PARTIAL GST credit note, cumulative across calls (flush-only; caller commits).

    * RMA mode (`returned_qty`): reverses the returned quantities, capped per product at
      what the invoice still has un-reversed (qty AND value), scaled down to `amount`
      when less was refunded (goodwill partials don't over-reverse output GST).
    * Value mode (`returned_qty=None`, `amount` given): a partial refund to source with
      no goods returned — reverses `amount` of value (qty 0) spread across products in
      proportion to their un-reversed value.

    One credit note per invoice: the first call creates the partial CN, later calls ADD
    lines to it and re-total it (a second CN would violate uq_credit_note_original and
    500). Once a FULL CN exists nothing is left to reverse → returns it unchanged.
    Returns None when there is nothing to reverse (no empty document burns a serial)."""
    if invoice.is_credit_note:
        raise ValueError("cannot issue a credit note against a credit note")
    await db.execute(select(func.pg_advisory_xact_lock(_CN_LOCK_NS, _lk(invoice.id))))
    existing = await _existing_credit_note(db, invoice)
    if existing is not None and not existing.is_partial:
        return existing

    inv_lines = (
        await db.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.id)
        )
    ).scalars().all()
    prior_lines = (
        (
            await db.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == existing.id)
            )
        ).scalars().all()
        if existing is not None else []
    )
    # Per-product: invoiced qty/value, already-reversed qty/value, and a template line.
    inv_qty: dict[int | None, float] = {}
    inv_val: dict[int | None, float] = {}
    tmpl: dict[int | None, InvoiceLine] = {}
    for ln in inv_lines:
        inv_qty[ln.product_id] = inv_qty.get(ln.product_id, 0.0) + float(ln.qty)
        inv_val[ln.product_id] = inv_val.get(ln.product_id, 0.0) + float(ln.line_total)
        tmpl.setdefault(ln.product_id, ln)
    rev_qty: dict[int | None, float] = {}
    rev_val: dict[int | None, float] = {}
    for ln in prior_lines:
        rev_qty[ln.product_id] = rev_qty.get(ln.product_id, 0.0) - float(ln.qty)
        rev_val[ln.product_id] = rev_val.get(ln.product_id, 0.0) - float(ln.line_total)

    def left_val(pid) -> float:
        return max(round(inv_val[pid] - rev_val.get(pid, 0.0), 2), 0.0)

    # (product, qty to reverse, gross value to reverse) before scaling to `amount`.
    portions: list[tuple[int | None, float, float]] = []
    if returned_qty is not None:
        for pid, q in returned_qty.items():
            if pid not in inv_qty or inv_qty[pid] <= 0 or float(q) <= 0:
                continue
            qty = min(float(q), max(inv_qty[pid] - rev_qty.get(pid, 0.0), 0.0))
            if qty <= 0:
                continue
            gross = min(inv_val[pid] * qty / inv_qty[pid], left_val(pid))
            portions.append((pid, qty, gross))
    elif amount is not None:
        portions = [(pid, 0.0, left_val(pid)) for pid in inv_val if left_val(pid) > 0]
    gross_total = sum(g for _, _, g in portions)
    if gross_total <= 0:
        return existing
    target = round(gross_total if amount is None else min(float(amount), gross_total), 2)
    if target <= 0:
        return existing

    if existing is None:
        note = _new_credit_note(invoice, partial=True)
        await _number_credit_note(db, note)
        db.add(note)
        await db.flush()
    else:
        note = existing
    # Allocate `target` across portions by gross value; the LAST portion takes the
    # remainder so the lines sum to target to the paisa. Each line total is split with
    # the same inclusive formula as the invoice, so taxable + tax == line total exactly.
    new_lines: list[InvoiceLine] = []
    running = 0.0
    for idx, (pid, qty, gross) in enumerate(portions):
        if idx < len(portions) - 1:
            lt = round(target * gross / gross_total, 2)
        else:
            lt = round(target - running, 2)
        running = round(running + lt, 2)
        if lt <= 0 and qty <= 0:
            continue
        t = tmpl[pid]
        taxable, c, s_, i = _inclusive_split(lt, float(t.gst_rate), invoice.is_interstate)
        new_lines.append(
            InvoiceLine(
                invoice_id=note.id, product_id=pid, hsn_code=t.hsn_code,
                description=((reason or "Credit note") + f" — {t.description or ''}")[:255],
                qty=-round(qty, 3), unit_price=float(t.unit_price),
                taxable_value=-taxable, gst_rate=float(t.gst_rate),
                cgst=-c, sgst=-s_, igst=-i, line_total=-lt,
            )
        )
    db.add_all(new_lines)
    _retotal(note, list(prior_lines) + new_lines)
    await db.flush()
    await db.refresh(note)
    return note
