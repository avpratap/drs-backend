from pydantic import BaseModel, ConfigDict


class ProductCreate(BaseModel):
    sku: str
    name: str
    description: str | None = None
    uom: str = "piece"
    pack_size: float = 1
    is_subscribable: bool = True
    perishable: bool = True
    hsn_code: str | None = None
    gst_rate: float = 0


class ProductOut(ProductCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
