"""Render a GST tax invoice to a PDF and store it; cache the URL on the invoice."""
import asyncio
import uuid
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.gst_states import state_code
from app.core.storage import PRIVATE_PREFIX, private_url, put_private_object
from app.models.delivery import DeliveryOrder
from app.models.invoice import Invoice, InvoiceLine
from app.models.order import Order
from app.models.profile import UserProfile
from app.models.shop import CustomerAccount, Shop


async def _buyer_details(db: AsyncSession, invoice: Invoice) -> tuple[str, str]:
    """(buyer name, billing/delivery address) for the invoice header — the supplied shop
    (delivery order / storefront order), with the registered legal name when known."""
    shop_id = None
    if invoice.delivery_order_id:
        do = await db.get(DeliveryOrder, invoice.delivery_order_id)
        shop_id = do.shop_id if do else None
    elif invoice.order_id:
        od = await db.get(Order, invoice.order_id)
        shop_id = od.shop_id if od else None
    shop = await db.get(Shop, shop_id) if shop_id else None
    account = await db.get(CustomerAccount, invoice.account_id)
    prof = await db.get(UserProfile, account.owner_user_id) if account else None
    name = (prof.legal_name or prof.display_name) if prof else None
    if not name and shop is not None and shop.name != "Home":
        name = shop.name
    address = ", ".join(
        p for p in ((shop.address if shop else None), (shop.pincode if shop else None)) if p
    )
    return name or "-", address or "-"


def _render(
    invoice: Invoice, lines: list[InvoiceLine], original: Invoice | None = None,
    buyer: tuple[str, str] = ("-", "-"),
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 50

    title = "CREDIT NOTE" if invoice.is_credit_note else "TAX INVOICE"
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, f"{settings.SELLER_LEGAL_NAME} — {title}")
    y -= 22
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"No: {invoice.invoice_no}    Date: {invoice.invoice_date}")
    y -= 14
    if invoice.is_credit_note and original is not None:
        # Rule 53: a credit note must reference the original tax invoice.
        c.drawString(
            50, y, f"Against tax invoice: {original.invoice_no} dated {original.invoice_date}"
        )
        y -= 14
    c.drawString(50, y, f"Seller GSTIN: {invoice.seller_gstin or '-'}    Buyer GSTIN: {invoice.buyer_gstin or '-'}")
    y -= 14
    c.drawString(50, y, f"Buyer: {buyer[0][:80]}")
    y -= 14
    c.drawString(50, y, f"Address: {buyer[1][:95]}")
    y -= 14
    pos_code = state_code(invoice.place_of_supply)
    c.drawString(50, y, f"Place of supply: {invoice.place_of_supply or '-'}"
                        f"{f' ({pos_code})' if pos_code else ''}"
                        f"  ({'Inter-state/IGST' if invoice.is_interstate else 'Intra-state/CGST+SGST'})")
    y -= 24

    def _table_header(top: float) -> float:
        """Draw the column header + rule and leave the cursor on the first body row,
        with the 8pt body font active. Reused on every (continuation) page."""
        c.setFont("Helvetica-Bold", 8)
        c.drawString(40, top, "HSN")
        c.drawString(85, top, "Description")
        c.drawRightString(270, top, "Qty")
        c.drawRightString(315, top, "Rate")
        c.drawRightString(365, top, "Taxable")
        c.drawRightString(395, top, "GST%")
        c.drawRightString(440, top, "CGST")
        c.drawRightString(485, top, "SGST")
        c.drawRightString(530, top, "IGST")
        c.drawRightString(575, top, "Total")
        top -= 4
        c.line(40, top, width - 20, top)
        top -= 13
        c.setFont("Helvetica", 8)
        return top

    y = _table_header(y)
    for ln in lines:
        c.drawString(40, y, (ln.hsn_code or "-")[:10])
        c.drawString(85, y, (ln.description or "")[:32])
        c.drawRightString(270, y, f"{float(ln.qty):g}")
        c.drawRightString(315, y, f"{float(ln.unit_price):.2f}")
        c.drawRightString(365, y, f"{float(ln.taxable_value):.2f}")
        c.drawRightString(395, y, f"{float(ln.gst_rate):g}")
        c.drawRightString(440, y, f"{float(ln.cgst):.2f}")
        c.drawRightString(485, y, f"{float(ln.sgst):.2f}")
        c.drawRightString(530, y, f"{float(ln.igst):.2f}")
        c.drawRightString(575, y, f"{float(ln.line_total):.2f}")
        y -= 13
        if y < 110:
            c.showPage()
            # showPage() resets the graphics state (incl. the font) to reportlab's
            # default — re-draw the header, which re-selects the body font, so
            # continued rows aren't rendered in the wrong (default 12pt) font (#73).
            y = _table_header(height - 60)

    y -= 6
    c.line(40, y, width - 20, y)
    y -= 16
    c.setFont("Helvetica-Bold", 10)
    for label, val in (
        ("Taxable value:", invoice.taxable_value),
        ("CGST:", invoice.cgst),
        ("SGST:", invoice.sgst),
        ("IGST:", invoice.igst),
        ("TOTAL:", invoice.total),
    ):
        c.drawRightString(470, y, label)
        c.drawRightString(575, y, f"{float(val):.2f}")
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


async def get_or_create_pdf(db: AsyncSession, invoice: Invoice) -> str:
    """Return a SHORT-LIVED signed URL for the invoice PDF (render + store privately on
    first use). The caller must have authorized the viewer for this invoice. The stored
    key carries a random component, so even the private key isn't guessable."""
    key = invoice.pdf_url
    if not (key and key.startswith(PRIVATE_PREFIX)):
        # No PDF yet, invalidated (credit note re-totalled), or a legacy PUBLIC URL —
        # (re)render into private storage.
        lines = (
            await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
        ).scalars().all()
        original = (
            await db.get(Invoice, invoice.original_invoice_id)
            if invoice.original_invoice_id else None
        )
        buyer = await _buyer_details(db, invoice)
        # reportlab render is CPU-bound and the upload does blocking I/O — run both off
        # the event loop so a PDF request can't stall every other coroutine.
        pdf = await asyncio.to_thread(_render, invoice, list(lines), original, buyer)
        name = invoice.invoice_no.replace("/", "_")
        key = await asyncio.to_thread(
            put_private_object,
            f"invoices/{invoice.fy}/{name}-{uuid.uuid4().hex}.pdf",
            pdf,
            "application/pdf",
        )
        invoice.pdf_url = key  # column holds the private KEY (never a public URL)
        await db.commit()
    return await asyncio.to_thread(private_url, key)
