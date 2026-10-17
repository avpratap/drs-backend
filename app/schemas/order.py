from pydantic import BaseModel, ConfigDict, Field


class OrderItemIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=100_000, allow_inf_nan=False)


class OrderCreate(BaseModel):
    items: list[OrderItemIn] = Field(min_length=1, max_length=100)
    address: str | None = Field(default=None, max_length=1024)
    # Delivery state/pincode — captured onto the D2C home shop so GST place-of-supply
    # (CGST+SGST vs IGST) is correct for interstate orders (#M12).
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=10)
    coupon_code: str | None = Field(default=None, max_length=32)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    qty: float
    unit_price: float
    line_total: float
    # GST breakdown for display. Prices are GST-INCLUSIVE: price_incl_tax == unit_price
    # and gst_amount is the tax portion backed out of the line. Indicative — the legal
    # GST invoice is computed separately in invoicing.py.
    gst_rate: float = 0
    gst_amount: float = 0
    price_incl_tax: float = 0


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_no: str
    shop_id: int
    order_type: str
    status: str
    subtotal: float
    discount: float = 0
    coupon_code: str | None = None
    total: float
    payment_status: str
    tax_total: float = 0
    total_incl_tax: float = 0
    items: list[OrderItemOut] = []
