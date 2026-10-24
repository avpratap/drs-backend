import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class DeliveryOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty_planned: float
    qty_delivered: float
    unit_price: float
    line_total: float
    status: str


class DeliveryOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    delivery_date: dt.date
    shop_id: int
    origin: str
    status: str
    failure_reason: str | None = None
    subtotal: float
    total: float
    amount_due: float
    items: list[DeliveryOrderItemOut] = []
    # Stop detail (populated in the serializer) so the delivery app knows where/who.
    shop_name: str | None = None
    address: str | None = None
    pincode: str | None = None
    lat: float | None = None
    lng: float | None = None
    contact_name: str | None = None
    contact_phone: str | None = None


class CutoffResult(BaseModel):
    schedule_run_id: int | None = None
    delivery_date: dt.date
    orders_generated: int
    skipped: str | None = None
    orders_removed: int = 0  # stale generated orders dropped (skip/pause since a run)
    failed: int = 0  # subscriptions that errored (run status "partial")


class DeliverItem(BaseModel):
    product_id: int
    qty_delivered: float = Field(ge=0, le=100_000, allow_inf_nan=False)


class PODIn(BaseModel):
    # Bounded to the proof_of_delivery column widths so an over-long value is a clean
    # 422, never a DataError 500 mid-delivery.
    recipient_name: str | None = Field(default=None, max_length=255)
    photo_url: str | None = Field(default=None, max_length=512)
    signature_url: str | None = Field(default=None, max_length=512)
    # Range-bounded so an out-of-range value is a clean 422, not a Numeric(9,6) 500.
    lat: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lng: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=255)


class ReturnIn(BaseModel):
    product_id: int
    qty_returned: float = Field(gt=0, le=100_000, allow_inf_nan=False)
    # Constrained to the documented reason codes (also keeps it within String(32)).
    reason: str = Field(pattern="^(damaged|expired|wrong_item|excess|refused)$")


class DeliverRequest(BaseModel):
    items: list[DeliverItem] = Field(min_length=1, max_length=200)
    pod: PODIn | None = None
    returns: list[ReturnIn] = Field(default=[], max_length=200)
    cash_collected: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    cash_method: str | None = Field(default=None, max_length=16)  # cash|upi|cheque
    cash_reference: str | None = Field(default=None, max_length=64)
