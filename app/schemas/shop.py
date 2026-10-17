from pydantic import BaseModel, ConfigDict, Field


class ShopCreate(BaseModel):
    # Bounded to the shops column widths so an over-long value is a clean 422, never a
    # DataError 500 (name/state/pincode are customer-reachable via POST /shops).
    name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=1024)
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=10)


class ShopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    name: str
    address: str | None = None
    state: str | None = None
    pincode: str | None = None
    status: str
