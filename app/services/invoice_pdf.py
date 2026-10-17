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

    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y, "HSN")
    c.drawString(110, y, "Description")
    c.drawString(320, y, "Qty")
    c.drawString(360, y, "Rate")
    c.drawString(410, y, "Taxable")
    c.drawString(470, y, "Tax")
    c.drawString(520, y, "Total")
    y -= 4
    c.line(50, y, width - 40, y)
    y -= 14
    c.setFont("Helvetica", 9)
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
            y = height - 60

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
