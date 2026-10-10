import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PHONE_RE = r"^\+?[0-9]{10,15}$"
PINCODE_RE = r"^[1-9][0-9]{5}$"  # Indian PIN: 6 digits, not starting 0
GSTIN_RE = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"


# ---------- Profile ----------
class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=512)
    date_of_birth: dt.date | None = None
    gender: Literal["male", "female", "other", "undisclosed"] | None = None
    locale: str | None = Field(default=None, max_length=12)
    alternate_phone: str | None = Field(default=None, pattern=PHONE_RE)
    gstin: str | None = Field(default=None, pattern=GSTIN_RE)
    legal_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    full_name: str | None = Field(default=None, max_length=255)
    notify_whatsapp: bool | None = None
    notify_sms: bool | None = None
    notify_push: bool | None = None
    notify_email: bool | None = None
    marketing_opt_in: bool | None = None
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)

    @field_validator("date_of_birth")
    @classmethod
    def _dob_sane(cls, v: dt.date | None) -> dt.date | None:
        # Hard sanity bounds; "not in the future" (tz-aware) is enforced in the service.
        if v is not None and not (dt.date(1900, 1, 1) <= v <= dt.date(2100, 1, 1)):
            raise ValueError("invalid date_of_birth")
        return v


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: int
    phone: str
    email: str | None = None
    full_name: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    date_of_birth: dt.date | None = None
    gender: str | None = None
    locale: str = "en-IN"
    alternate_phone: str | None = None
    gstin: str | None = None
    legal_name: str | None = None
    notify_whatsapp: bool = True
    notify_sms: bool = True
    notify_push: bool = True
    notify_email: bool = False
    marketing_opt_in: bool = False
    quiet_hours_start: int | None = None
    quiet_hours_end: int | None = None


# ---------- Address ----------
class AddressIn(BaseModel):
    label: str | None = Field(default=None, max_length=40)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_phone: str | None = Field(default=None, pattern=PHONE_RE)
    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    landmark: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=1, max_length=120)
    pincode: str = Field(pattern=PINCODE_RE)
    country: str = Field(default="IN", min_length=2, max_length=2)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    is_default: bool = False


class AddressUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=40)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_phone: str | None = Field(default=None, pattern=PHONE_RE)
    line1: str | None = Field(default=None, min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    landmark: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, min_length=1, max_length=120)
    state: str | None = Field(default=None, min_length=1, max_length=120)
    pincode: str | None = Field(default=None, pattern=PINCODE_RE)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    is_default: bool | None = None


class AddressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    label: str | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    line1: str
    line2: str | None = None
    landmark: str | None = None
    city: str
    state: str
    pincode: str
    country: str
    is_default: bool
    status: str


# ---------- Payment method ----------
class PaymentMethodIn(BaseModel):
    method_type: Literal["upi", "card", "netbanking"]
    gateway_customer_id: str | None = Field(default=None, max_length=64)
    gateway_token: str | None = Field(default=None, max_length=128)
    display: str | None = Field(default=None, max_length=64)
    brand: str | None = Field(default=None, max_length=32)
    expiry_month: int | None = Field(default=None, ge=1, le=12)
    expiry_year: int | None = Field(default=None, ge=2024, le=2099)
    is_default: bool = False


class PaymentMethodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    method_type: str
    gateway: str
    display: str | None = None
    brand: str | None = None
    expiry_month: int | None = None
    expiry_year: int | None = None
    is_default: bool
    status: str


# ---------- Mandate ----------
class MandateIn(BaseModel):
    payment_method_id: int | None = None
    max_amount: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    frequency: Literal["daily", "weekly", "monthly", "as_presented"] = "as_presented"
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None


class MandateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    payment_method_id: int | None = None
    gateway: str
    gateway_mandate_id: str | None = None
    max_amount: float
    frequency: str
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    status: str


# ---------- Consent ----------
class ConsentIn(BaseModel):
    purpose: Literal[
        "whatsapp_optin", "marketing_sms", "marketing_email", "data_processing"
    ]
    action: Literal["granted", "withdrawn"]


class ConsentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    purpose: str
    action: str
    source: str
    created_at: dt.datetime
