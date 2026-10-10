import datetime as dt

from pydantic import BaseModel, ConfigDict


class SubscriptionCreate(BaseModel):
    shop_id: int


class ItemIn(BaseModel):
    product_id: int
    default_qty: float
    frequency: str = "daily"  # daily | alternate | weekly
    interval_days: int | None = None
    days_of_week: str | None = None  # csv "1,3,5" (Mon=1)


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
    override_qty: float  # 0 = skip
    note: str | None = None


class PlanLine(BaseModel):
    product_id: int
    qty: float


class PlanOut(BaseModel):
    for_date: dt.date
    lines: list[PlanLine]
