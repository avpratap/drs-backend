import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class InvoiceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int | None = None
    hsn_code: str | None = None
    description: str | None = None
    qty: float
    unit_price: float
    taxable_value: float
    gst_rate: float
    cgst: float
    sgst: float
    igst: float
    line_total: float


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_no: str
    fy: str
    account_id: int
    delivery_order_id: int | None = None
    order_id: int | None = None
    invoice_date: dt.date
    seller_gstin: str | None = None
    buyer_gstin: str | None = None
    place_of_supply: str | None = None
    is_interstate: bool
    taxable_value: float
    cgst: float
    sgst: float
    igst: float
    total: float
    is_credit_note: bool
    original_invoice_id: int | None = None
    status: str
    lines: list[InvoiceLineOut] = []


class CreditNoteIn(BaseModel):
    reason: str | None = Field(default=None, max_length=255)
