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
