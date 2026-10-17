"""GST invoice + credit-note generation.

- Financial year = Apr 1 - Mar 31; invoice numbers are sequential per FY, serialized
  with an advisory lock so two concurrent generations can't collide.
- Place of supply (buyer state) vs seller state decides CGST+SGST vs IGST.
- One tax invoice per delivery order (idempotent); credit notes mirror an invoice
  with negated amounts for returns/refunds.
"""
import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.delivery import DeliveryOrder, DeliveryOrderItem
from app.models.invoice import Invoice, InvoiceLine
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.profile import Address, UserProfile
from app.models.shop import CustomerAccount, Shop

_SEQ_LOCK_NS = 555001
_INV_LOCK_NS = 555002  # serialize invoice generation per delivery order
_CN_LOCK_NS = 555003  # serialize credit-note generation per source invoice
_ORD_INV_LOCK_NS = 555004  # serialize invoice generation per one-time order


def fy_for_date(d: dt.date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


async def _next_seq(db: AsyncSession, fy: str) -> int:
    await db.execute(select(func.pg_advisory_xact_lock(_SEQ_LOCK_NS, int(fy[:4]))))
    last = await db.scalar(select(func.max(Invoice.seq_no)).where(Invoice.fy == fy))
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


def _split(taxable: float, rate: float, interstate: bool) -> tuple[float, float, float]:
    total_tax = round(taxable * rate / 100, 2)
    if interstate:
        return 0.0, 0.0, total_tax
    # Halve from the single rounded total so CGST+SGST always equals it (no paisa drift).
    cgst = round(total_tax / 2, 2)
    return cgst, round(total_tax - cgst, 2), 0.0


async def generate_for_delivery(db: AsyncSession, order: DeliveryOrder) -> Invoice:
    # Serialize concurrent generations for this order so the existence check below
    # is reliable (partial-unique index on delivery_order_id is the DB backstop).
    await db.execute(select(func.pg_advisory_xact_lock(_INV_LOCK_NS, order.id)))
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
    today = dt.datetime.now(dt.UTC).date()
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

    tax_val = cgst = sgst = igst = total = 0.0
    for it in rows:
        qty = float(it.qty_delivered or 0)
        if qty <= 0:
            continue
        product = await db.get(Product, it.product_id)
        rate = float(product.gst_rate) if product else 0.0
        taxable = round(float(it.unit_price) * qty, 2)
        c, s, i = _split(taxable, rate, interstate)
        line_total = round(taxable + c + s + i, 2)
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
    shop = await db.get(Shop, order.shop_id)
    if shop is not None and shop.state:
        return shop.state
    state = await db.scalar(
        select(Address.state).where(
            Address.account_id == order.account_id,
            Address.status == "active",
            Address.is_default.is_(True),
        ).limit(1)
    )
    return state or settings.SELLER_STATE


async def generate_for_order(db: AsyncSession, order: Order) -> Invoice:
    """GST tax invoice for a one-time (D2C storefront) Order. Idempotent per order."""
    await db.execute(select(func.pg_advisory_xact_lock(_ORD_INV_LOCK_NS, order.id)))
    existing = await db.scalar(
        select(Invoice).where(Invoice.order_id == order.id, Invoice.is_credit_note.is_(False))
    )
    if existing is not None:
        return existing

    place = await _order_place_of_supply(db, order)
    interstate = place.strip().lower() != settings.SELLER_STATE.strip().lower()
    today = dt.datetime.now(dt.UTC).date()
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

    rows = (
        await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    ).scalars().all()
    # Apportion any order-level discount across lines so taxable value matches what was
    # actually charged (order.total is subtotal - discount).
    subtotal = float(order.subtotal) or 0.0
    disc_factor = (float(order.total) / subtotal) if subtotal > 0 else 1.0
    tax_val = cgst = sgst = igst = total = 0.0
    for it in rows:
        qty = float(it.qty)
        product = await db.get(Product, it.product_id)
        rate = float(product.gst_rate) if product else 0.0
        taxable = round(float(it.line_total) * disc_factor, 2)
        c, s, i = _split(taxable, rate, interstate)
        line_total = round(taxable + c + s + i, 2)
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
    await db.execute(select(func.pg_advisory_xact_lock(_CN_LOCK_NS, invoice.id)))
    existing = await db.scalar(
        select(Invoice).where(
            Invoice.original_invoice_id == invoice.id, Invoice.is_credit_note.is_(True)
        )
    )
    if existing is not None:
        return existing

    fy = fy_for_date(dt.datetime.now(dt.UTC).date())
    seq = await _next_seq(db, fy)
    note = Invoice(
        invoice_no=f"{settings.INVOICE_PREFIX}/CN/{fy}/{seq:06d}",
        fy=fy,
        seq_no=seq,
        account_id=invoice.account_id,
        delivery_order_id=invoice.delivery_order_id,
        order_id=invoice.order_id,
        invoice_date=dt.datetime.now(dt.UTC).date(),
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

    lines = (
        await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
    ).scalars().all()
    for ln in lines:
        db.add(
            InvoiceLine(
                invoice_id=note.id,
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
        )
    await db.commit()
    await db.refresh(note)
    return note
