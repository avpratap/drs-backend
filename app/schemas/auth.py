import re

from pydantic import BaseModel, ConfigDict, field_validator


class OTPRequest(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def _valid_phone(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"\+?\d{10,15}", v):
            raise ValueError("Invalid phone number (use digits, optional leading +)")
        return v


class OTPRequestResponse(BaseModel):
    detail: str
    dev_otp: str | None = None  # populated only in non-prod, for testing


class OTPVerify(BaseModel):
    phone: str
    otp: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    phone: str
    role: str
    full_name: str | None = None
    status: str
