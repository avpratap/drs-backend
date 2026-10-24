from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import reject_nulls


class ProductCreate(BaseModel):
    # Bounded to the products column widths (clean 422 instead of a DB DataError 500).
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    uom: str = Field(default="piece", max_length=16)
    pack_size: float = Field(default=1, gt=0, le=100_000, allow_inf_nan=False)
    is_subscribable: bool = True
    perishable: bool = True
    hsn_code: str | None = Field(default=None, max_length=16)
    # Column is Numeric(4,2): 100+ overflowed (500) and negatives were accepted.
    gst_rate: float = Field(default=0, ge=0, le=99.99, allow_inf_nan=False)  # Numeric(4,2)
    category: str | None = Field(default=None, max_length=64)
    image_url: str | None = Field(default=None, max_length=512)


class ProductUpdate(BaseModel):
    # Bounded to the products column widths (clean 422 instead of a DB DataError 500),
    # mirroring ProductCreate.
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    uom: str | None = Field(default=None, max_length=16)
    pack_size: float | None = Field(default=None, gt=0, le=100_000, allow_inf_nan=False)
    is_subscribable: bool | None = None
    perishable: bool | None = None
    hsn_code: str | None = Field(default=None, max_length=16)
    gst_rate: float | None = Field(default=None, ge=0, le=99.99, allow_inf_nan=False)
    category: str | None = Field(default=None, max_length=64)
    image_url: str | None = Field(default=None, max_length=512)

    _v_nulls = reject_nulls("name", "uom", "pack_size", "is_subscribable", "perishable", "gst_rate")


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
