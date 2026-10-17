import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SubscriptionCreate(BaseModel):
    # B2B subscribers pass one of their shop_ids. Retail customers may omit it; a "home
    # shop" is auto-provisioned under their account from the optional address fields.
    shop_id: int | None = None
    home_name: str | None = Field(default=None, max_length=255)
    home_address: str | None = Field(default=None, max_length=1024)
    home_state: str | None = Field(default=None, max_length=120)
    home_pincode: str | None = Field(default=None, max_length=10)


class ItemIn(BaseModel):
    product_id: int
    default_qty: float = Field(ge=0, le=100_000, allow_inf_nan=False)
    frequency: str = "daily"  # daily | alternate | weekly
    interval_days: int | None = Field(default=None, ge=1, le=365)
    # csv of ISO weekdays, Mon=1..Sun=7, e.g. "1,3,5". Bounded + format-checked below.
    days_of_week: str | None = Field(default=None, max_length=32, pattern=r"^[1-7](,[1-7])*$")

    @model_validator(mode="after")
    def _check_cadence(self) -> "ItemIn":
        # Reject cadences that the cutoff would otherwise mis-handle: an unknown frequency
        # or a weekly line with no days silently materializes EVERY day (over-billing);
        # a malformed days string would drop the subscription from every cutoff.
        if self.frequency not in ("daily", "alternate", "weekly"):
            raise ValueError("frequency must be one of: daily, alternate, weekly")
        if self.frequency == "weekly" and not self.days_of_week:
            raise ValueError("weekly frequency requires days_of_week (e.g. '1,3,5')")
        if self.frequency == "alternate" and self.interval_days is None:
            raise ValueError("alternate frequency requires interval_days")
        return self


class ItemOut(ItemIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    shop_id: int
    account_id: int
    status: str
    start_date: dt.date
    end_date: dt.date | None = None


class SubscriptionDetail(SubscriptionOut):
    items: list[ItemOut] = []


class OverrideIn(BaseModel):
    product_id: int
    for_date: dt.date
    override_qty: float = Field(ge=0, le=100_000, allow_inf_nan=False)  # 0 = skip
    note: str | None = Field(default=None, max_length=255)  # bounded to the column width


class PlanLine(BaseModel):
    product_id: int
    qty: float


class PlanOut(BaseModel):
    for_date: dt.date
    lines: list[PlanLine]
