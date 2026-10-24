from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user, require_role
from app.deps.pagination import Page, pagination
from app.models.delivery import DeliveryOrder
from app.models.invoice import Invoice, InvoiceLine
from app.models.shop import CustomerAccount
from app.models.user import User
from app.schemas.invoice import CreditNoteIn, InvoiceLineOut, InvoiceOut
from app.services import invoice_pdf, invoicing

router = APIRouter(tags=["invoices"])


class InvoicePdfOut(BaseModel):
    invoice_no: str
    pdf_url: str


async def _my_account_ids(db: AsyncSession, user: User) -> set[int]:
    res = await db.execute(
        select(CustomerAccount.id).where(CustomerAccount.owner_user_id == user.id)
    )
    return set(res.scalars().all())


async def _detail(db: AsyncSession, inv: Invoice) -> InvoiceOut:
    lines = (
        await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id))
    ).scalars().all()
    out = InvoiceOut.model_validate(inv)
    out.lines = [InvoiceLineOut.model_validate(ln) for ln in lines]
    return out


async def _details(db: AsyncSession, invoices: list[Invoice]) -> list[InvoiceOut]:
    """Build invoice views with a single batched line query (no N+1)."""
    if not invoices:
        return []
    ids = [i.id for i in invoices]
    rows = (
        await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id.in_(ids)))
    ).scalars().all()
    by_inv: dict[int, list[InvoiceLineOut]] = {}
    for ln in rows:
        by_inv.setdefault(ln.invoice_id, []).append(InvoiceLineOut.model_validate(ln))
    views = []
    for inv in invoices:
        v = InvoiceOut.model_validate(inv)
        v.lines = by_inv.get(inv.id, [])
        views.append(v)
    return views


@router.get("/invoices", response_model=list[InvoiceOut], operation_id="list_invoices")
async def list_invoices(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Page = Depends(pagination),
):
    query = select(Invoice).order_by(Invoice.id.desc())
    if user.role != "admin":
        ids = await _my_account_ids(db, user)
        if not ids:
            return []
        query = query.where(Invoice.account_id.in_(ids))
    invoices = (
        await db.execute(query.limit(page.limit).offset(page.offset))
    ).scalars().all()
    return await _details(db, list(invoices))


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut, operation_id="get_invoice")
async def get_invoice(
    invoice_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    inv = await db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    if user.role != "admin" and inv.account_id not in await _my_account_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return await _detail(db, inv)


@router.get("/invoices/{invoice_id}/pdf", operation_id="get_invoice_pdf", response_model=InvoicePdfOut)
async def get_invoice_pdf(
    invoice_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    inv = await db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    if user.role != "admin" and inv.account_id not in await _my_account_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    url = await invoice_pdf.get_or_create_pdf(db, inv)
    return {"invoice_no": inv.invoice_no, "pdf_url": url}


@router.post(
    "/admin/invoices/generate",
    response_model=InvoiceOut,
    operation_id="generate_invoice",
    dependencies=[Depends(require_role("admin"))],
)
async def generate_invoice(delivery_order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(DeliveryOrder, delivery_order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Delivery order not found")
    if order.status not in ("delivered", "partially_delivered"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Invoice can only be generated for a delivered order"
        )
    try:
        inv = await invoicing.generate_for_delivery(db, order)
    except invoicing.NothingToInvoice:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Nothing was supplied on this order; no invoice to issue"
        ) from None
    return await _detail(db, inv)


@router.post(
    "/admin/invoices/{invoice_id}/credit-note",
    response_model=InvoiceOut,
    operation_id="generate_credit_note",
    dependencies=[Depends(require_role("admin"))],
)
async def generate_credit_note(
    invoice_id: int, data: CreditNoteIn, db: AsyncSession = Depends(get_db)
):
    inv = await db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    try:
        note = await invoicing.generate_credit_note(db, inv, data.reason)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _detail(db, note)
