from pydantic import BaseModel, ConfigDict


class ShopCreate(BaseModel):
    name: str
    address: str | None = None


class ShopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    name: str
    address: str | None = None
    status: str
