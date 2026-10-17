from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    # Bounded to the products column widths (clean 422 instead of a DB DataError 500).
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    uom: str = Field(default="piece", max_length=16)
    pack_size: float = 1
    is_subscribable: bool = True
    perishable: bool = True
    hsn_code: str | None = Field(default=None, max_length=16)
    gst_rate: float = 0
    category: str | None = Field(default=None, max_length=64)
    image_url: str | None = Field(default=None, max_length=512)


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
    avg_rating: float = 0
    rating_count: int = 0
    weight_kg: float | None = None
    length_cm: float | None = None
    width_cm: float | None = None
    height_cm: float | None = None
