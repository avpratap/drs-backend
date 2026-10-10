from pydantic import BaseModel, ConfigDict


class OrderItemIn(BaseModel):
    product_id: int
    qty: float


class OrderCreate(BaseModel):
    items: list[OrderItemIn]
    address: str | None = None


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty: float
    unit_price: float
    line_total: float


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_no: str
    shop_id: int
    order_type: str
    status: str
    subtotal: float
    total: float
    payment_status: str
    items: list[OrderItemOut] = []
