"""Render a GST tax invoice to a PDF and store it; cache the URL on the invoice."""
import asyncio
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.storage import put_object
from app.models.invoice import Invoice, InvoiceLine


def _render(invoice: Invoice, lines: list[InvoiceLine]) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 50

    title = "CREDIT NOTE" if invoice.is_credit_note else "TAX INVOICE"
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, f"{settings.SELLER_LEGAL_NAME} — {title}")
    y -= 22
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Invoice: {invoice.invoice_no}    Date: {invoice.invoice_date}")
    y -= 14
    c.drawString(50, y, f"Seller GSTIN: {invoice.seller_gstin or '-'}    Buyer GSTIN: {invoice.buyer_gstin or '-'}")
    y -= 14
    c.drawString(50, y, f"Place of supply: {invoice.place_of_supply or '-'}"
                        f"  ({'Inter-state/IGST' if invoice.is_interstate else 'Intra-state/CGST+SGST'})")
    y -= 24

    def _table_header(top: float) -> float:
        """Draw the column header + rule and leave the cursor on the first body row,
        with the 9pt body font active. Reused on every (continuation) page."""
        c.setFont("Helvetica-Bold", 9)
        c.drawString(50, top, "HSN")
        c.drawString(110, top, "Description")
        c.drawString(320, top, "Qty")
        c.drawString(360, top, "Rate")
        c.drawString(410, top, "Taxable")
        c.drawString(470, top, "Tax")
        c.drawString(520, top, "Total")
        top -= 4
        c.line(50, top, width - 40, top)
        top -= 14
        c.setFont("Helvetica", 9)
        return top

    y = _table_header(y)
    for ln in lines:
        tax = float(ln.cgst) + float(ln.sgst) + float(ln.igst)
        c.drawString(50, y, (ln.hsn_code or "-")[:10])
        c.drawString(110, y, (ln.description or "")[:34])
        c.drawRightString(345, y, f"{float(ln.qty):g}")
        c.drawRightString(400, y, f"{float(ln.unit_price):.2f}")
        c.drawRightString(460, y, f"{float(ln.taxable_value):.2f}")
        c.drawRightString(510, y, f"{tax:.2f}")
        c.drawRightString(565, y, f"{float(ln.line_total):.2f}")
        y -= 14
        if y < 90:
            c.showPage()
            # showPage() resets the graphics state (incl. the font) to reportlab's
            # default — re-draw the header, which re-selects the 9pt body font, so
            # continued rows aren't rendered in the wrong (default 12pt) font (#73).
            y = _table_header(height - 60)

    y -= 6
    c.line(50, y, width - 40, y)
    y -= 16
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(460, y, "Taxable:")
    c.drawRightString(565, y, f"{float(invoice.taxable_value):.2f}")
    y -= 14
    c.drawRightString(460, y, "CGST+SGST+IGST:")
    c.drawRightString(565, y, f"{float(invoice.cgst) + float(invoice.sgst) + float(invoice.igst):.2f}")
    y -= 14
    c.drawRightString(460, y, "TOTAL:")
    c.drawRightString(565, y, f"{float(invoice.total):.2f}")
    c.showPage()
    c.save()
    return buf.getvalue()


async def get_or_create_pdf(db: AsyncSession, invoice: Invoice) -> str:
    if invoice.pdf_url:
        return invoice.pdf_url
    lines = (
        await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id))
    ).scalars().all()
    # reportlab render is CPU-bound and the upload does blocking I/O — run both off the
    # event loop so a PDF request can't stall every other coroutine.
    pdf = await asyncio.to_thread(_render, invoice, list(lines))
    key = f"invoices/{invoice.fy}/{invoice.invoice_no.replace('/', '_')}.pdf"
    url = await asyncio.to_thread(put_object, key, pdf, "application/pdf")
    invoice.pdf_url = url
    await db.commit()
    return url
