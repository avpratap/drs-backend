from pydantic import BaseModel, ConfigDict, Field


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
    category: str | None = None
    image_url: str | None = None


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    uom: str | None = None
    pack_size: float | None = Field(default=None, gt=0)
    is_subscribable: bool | None = None
    perishable: bool | None = None
    hsn_code: str | None = None
    gst_rate: float | None = Field(default=None, ge=0, le=100)
    category: str | None = None
    image_url: str | None = None


class ProductOut(ProductCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
