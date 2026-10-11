from pydantic import BaseModel, Field


class PaymentCreate(BaseModel):
    # Required for wallet_topup; ignored for order_payment (server uses order.total).
    amount: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    purpose: str  # wallet_topup | order_payment
    order_id: int | None = None  # required for order_payment


class PaymentCreateOut(BaseModel):
    payment_id: int
    gateway_order_id: str
    amount: float
    key_id: str  # Razorpay key id for the client checkout ("" in dev stub)


class PaymentVerify(BaseModel):
    gateway_order_id: str
    gateway_payment_id: str
    signature: str  # use "dev" in dev stub mode


class RefundIn(BaseModel):
    # None -> refund the full remaining amount.
    amount: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    reason: str | None = Field(default=None, max_length=255)
