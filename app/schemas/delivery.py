import datetime as dt

from pydantic import BaseModel, ConfigDict


class DeliveryOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty_planned: float
    qty_delivered: float
    status: str


class DeliveryOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    delivery_date: dt.date
    shop_id: int
    origin: str
    status: str
    items: list[DeliveryOrderItemOut] = []


class CutoffResult(BaseModel):
    schedule_run_id: int
    delivery_date: dt.date
    orders_generated: int


class DeliverItem(BaseModel):
    product_id: int
    qty_delivered: float


class PODIn(BaseModel):
    recipient_name: str | None = None
    photo_url: str | None = None
    signature_url: str | None = None
    lat: float | None = None
    lng: float | None = None
    note: str | None = None


class ReturnIn(BaseModel):
    product_id: int
    qty_returned: float
    reason: str  # damaged|expired|wrong_item|excess|refused


class DeliverRequest(BaseModel):
    items: list[DeliverItem]
    pod: PODIn | None = None
    returns: list[ReturnIn] = []
    cash_collected: float | None = None
    cash_method: str | None = None  # cash|upi|cheque
    cash_reference: str | None = None
