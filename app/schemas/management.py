import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class DeliveryAssignmentIn(BaseModel):
    delivery_id: int
    shop_id: int


class DeliveryAssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    delivery_id: int
    shop_id: int
    active: bool


class PriceTierIn(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    tier_type: str = Field(default="retail", pattern="^(retail|b2b)$")


class PriceTierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    tier_type: str


class ProductPriceIn(BaseModel):
    product_id: int
    price_tier_id: int
    unit_price: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    min_qty: float = Field(default=1, gt=0, le=1_000_000, allow_inf_nan=False)
    valid_from: dt.date
    valid_to: dt.date | None = None


class ProductPriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    price_tier_id: int
    unit_price: float
    min_qty: float
    valid_from: dt.date
    valid_to: dt.date | None = None


class TierAssignIn(BaseModel):
    price_tier_id: int


class UserAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    phone: str
    email: str | None = None
    full_name: str | None = None
    role: str
    status: str


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|customer|subscriber|delivery)$")
    status: str | None = Field(default=None, pattern="^(active|suspended)$")


class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=32)
    discount_type: str = Field(pattern="^(percent|flat)$")
    value: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    min_order_value: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)
    max_discount: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    usage_limit: int | None = Field(default=None, ge=1)
    per_user_limit: int | None = Field(default=None, ge=1)


class CouponOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    discount_type: str
    value: float
    min_order_value: float
    max_discount: float | None = None
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    usage_limit: int | None = None
    used_count: int
    per_user_limit: int | None = None
    active: bool


class CreditSettleIn(BaseModel):
    amount: float = Field(gt=0, le=10_000_000, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=255)


class WalletAdjustIn(BaseModel):
    amount: float = Field(le=10_000_000, ge=-10_000_000, allow_inf_nan=False)  # signed
    reason: str = Field(min_length=2, max_length=32)
    note: str | None = Field(default=None, max_length=255)


class AccountBillingOut(BaseModel):
    account_id: int
    billing_mode: str
    wallet_balance: float
    credit_outstanding: float


class ShopPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    address: str | None = None
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=10)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")
