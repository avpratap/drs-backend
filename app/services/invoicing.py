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
from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.fulfillment import Return
from app.models.invoice import Invoice, InvoiceLine
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.profile import Address, UserProfile
from app.models.shop import CustomerAccount, Shop


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

    place = await _place_of_supply(db, order)
    interstate = place.strip().lower() != settings.SELLER_STATE.strip().lower()
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

    tax_val = cgst = sgst = igst = total = 0.0
    for it in rows:
        qty = round(float(it.qty_delivered or 0) - returned.get(it.product_id, 0.0), 3)
        if qty <= 0:
            continue
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

    place = await _order_place_of_supply(db, order)
    interstate = place.strip().lower() != settings.SELLER_STATE.strip().lower()
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


async def generate_credit_note(db: AsyncSession, invoice: Invoice, reason: str | None) -> Invoice:
    if invoice.is_credit_note:
        raise ValueError("cannot issue a credit note against a credit note")
    await db.execute(select(func.pg_advisory_xact_lock(_CN_LOCK_NS, _lk(invoice.id))))
    # ONE credit note per invoice (uq_credit_note_original partial unique index). Find any
    # existing CN for this invoice:
    #   * already a FULL reversal (is_partial=False) -> idempotent, return it.
    #   * a PARTIAL (RMA) CN -> UPGRADE it in place to the full reversal. The unique index
    #     forbids inserting a second CN, so a full refund-to-source can't add one on top;
    #     upgrading reverses the invoice exactly once (no GST over- or under-reversal).
    existing = await db.scalar(
        select(Invoice).where(
            Invoice.original_invoice_id == invoice.id,
            Invoice.is_credit_note.is_(True),
        )
    )
    if existing is not None and not existing.is_partial:
        return existing

    orig_lines = (
        await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
    ).scalars().all()

    def _full_negation_lines(target_id: int) -> list[InvoiceLine]:
        return [
            InvoiceLine(
                invoice_id=target_id,
                product_id=ln.product_id,
                hsn_code=ln.hsn_code,
                description=(reason or "Credit note") + f" — {ln.description or ''}",
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

    if existing is not None:
        # Upgrade the partial CN to the FULL reversal: swap its lines + header totals for
        # the exact negation of the invoice and clear is_partial.
        await db.execute(delete(InvoiceLine).where(InvoiceLine.invoice_id == existing.id))
        existing.is_partial = False
        existing.taxable_value = round(-float(invoice.taxable_value), 2)
        existing.cgst = round(-float(invoice.cgst), 2)
        existing.sgst = round(-float(invoice.sgst), 2)
        existing.igst = round(-float(invoice.igst), 2)
        existing.total = round(-float(invoice.total), 2)
        for line in _full_negation_lines(existing.id):
            db.add(line)
        await db.commit()
        await db.refresh(existing)
        return existing

    fy = fy_for_date(_today())
    seq = await _next_seq(db, fy, is_credit_note=True)
    note = Invoice(
        invoice_no=f"{settings.INVOICE_PREFIX}/CN/{fy}/{seq:06d}",
        fy=fy,
        seq_no=seq,
        account_id=invoice.account_id,
        delivery_order_id=invoice.delivery_order_id,
        order_id=invoice.order_id,
        invoice_date=_today(),
        seller_gstin=invoice.seller_gstin,
        buyer_gstin=invoice.buyer_gstin,
        place_of_supply=invoice.place_of_supply,
        is_interstate=invoice.is_interstate,
        is_credit_note=True,
        original_invoice_id=invoice.id,
        taxable_value=round(-float(invoice.taxable_value), 2),
        cgst=round(-float(invoice.cgst), 2),
        sgst=round(-float(invoice.sgst), 2),
        igst=round(-float(invoice.igst), 2),
        total=round(-float(invoice.total), 2),
        status="issued",
    )
    db.add(note)
    await db.flush()
    for line in _full_negation_lines(note.id):
        db.add(line)
    await db.commit()
    await db.refresh(note)
    return note


async def generate_credit_note_for_lines(
    db: AsyncSession, invoice: Invoice, returned_qty: dict[int, float], reason: str | None,
    amount: float | None = None,
) -> Invoice:
    """Partial GST credit note for an RMA return. Reverses the RETURNED quantities,
    allocating each product's returned qty ACROSS that product's invoice lines (so a
    product spanning >1 line isn't over-reversed), and — when `amount` is given and is
    less than the returned-lines value — scales the whole reversal to the amount actually
    refunded (goodwill partials don't over-reverse output GST). Marked is_partial so it
    won't be mistaken for the full-reversal credit note. Flush-only: the caller owns the
    commit (and the single-shot RMA status guard is what prevents duplicates)."""
    if invoice.is_credit_note:
        raise ValueError("cannot issue a credit note against a credit note")
    fy = fy_for_date(_today())
    seq = await _next_seq(db, fy, is_credit_note=True)
    note = Invoice(
        invoice_no=f"{settings.INVOICE_PREFIX}/CN/{fy}/{seq:06d}",
        fy=fy, seq_no=seq, account_id=invoice.account_id,
        delivery_order_id=invoice.delivery_order_id, order_id=invoice.order_id,
        invoice_date=_today(),
        seller_gstin=invoice.seller_gstin, buyer_gstin=invoice.buyer_gstin,
        place_of_supply=invoice.place_of_supply, is_interstate=invoice.is_interstate,
        is_credit_note=True, is_partial=True, original_invoice_id=invoice.id, status="issued",
    )
    db.add(note)
    await db.flush()

    lines = (
        await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
    ).scalars().all()
    # Allocate each product's returned qty across its lines (a product may appear on >1).
    remaining = {pid: float(q) for pid, q in returned_qty.items()}
    portions: list[tuple[InvoiceLine, float, float]] = []  # (line, alloc_qty, frac)
    gross_value = 0.0
    for ln in lines:
        line_qty = float(ln.qty)
        rem = remaining.get(ln.product_id, 0.0)
        if rem <= 0 or line_qty <= 0:
            continue
        alloc = min(rem, line_qty)
        remaining[ln.product_id] = rem - alloc
        frac = alloc / line_qty
        gross_value += float(ln.line_total) * frac
        portions.append((ln, alloc, frac))
    # Scale to the actually-refunded amount if it is less than the returned-lines value.
    scale = 1.0
    if amount is not None and gross_value > 0:
        scale = min(float(amount), gross_value) / gross_value

    tax_val = cgst = sgst = igst = total = 0.0
    for ln, alloc, frac in portions:
        f = frac * scale
        taxable = round(-float(ln.taxable_value) * f, 2)
        c = round(-float(ln.cgst) * f, 2)
        s = round(-float(ln.sgst) * f, 2)
        i = round(-float(ln.igst) * f, 2)
        lt = round(-float(ln.line_total) * f, 2)
        db.add(
            InvoiceLine(
                invoice_id=note.id, product_id=ln.product_id, hsn_code=ln.hsn_code,
                description=(reason or "Credit note") + f" — {ln.description or ''}",
                qty=-round(alloc, 3), unit_price=float(ln.unit_price),
                taxable_value=taxable, gst_rate=float(ln.gst_rate),
                cgst=c, sgst=s, igst=i, line_total=lt,
            )
        )
        tax_val += taxable
        cgst += c
        sgst += s
        igst += i
        total += lt
    note.taxable_value = round(tax_val, 2)
    note.cgst = round(cgst, 2)
    note.sgst = round(sgst, 2)
    note.igst = round(igst, 2)
    note.total = round(total, 2)
    await db.flush()
    await db.refresh(note)
    return note
