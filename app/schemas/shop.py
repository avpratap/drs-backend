from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.gst_states import validate_state
from app.schemas.profile import PINCODE_RE


class ShopCreate(BaseModel):
    # Bounded to the shops column widths so an over-long value is a clean 422, never a
    # DataError 500 (name/state/pincode are customer-reachable via POST /shops).
    name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=1024)
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, pattern=PINCODE_RE)

    _v_state = field_validator("state")(lambda v: validate_state(v))


class ShopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    name: str
    address: str | None = None
    state: str | None = None
    pincode: str | None = None
    status: str
