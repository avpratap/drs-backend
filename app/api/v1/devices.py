"""FCM device-token registration (customer apps register for push)."""
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.models.device_token import DeviceToken
from app.models.user import User

router = APIRouter(prefix="/account/device-tokens", tags=["devices"])


class DeviceTokenIn(BaseModel):
    token: str = Field(min_length=8, max_length=512)
    platform: str = Field(default="android", pattern="^(ios|android|web)$")


class DeviceTokenOut(BaseModel):
    token: str
    platform: str
    active: bool


@router.post("", response_model=DeviceTokenOut, operation_id="register_device_token")
async def register_device_token(
    data: DeviceTokenIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Register (or re-activate) this device for push. Idempotent on the token:
    re-registering reassigns it to the caller and marks it active."""
    row = await db.scalar(select(DeviceToken).where(DeviceToken.token == data.token))
    if row is None:
        row = DeviceToken(user_id=user.id, token=data.token, platform=data.platform, active=True)
        db.add(row)
    else:
        row.user_id = user.id
        row.platform = data.platform
        row.active = True
    await db.commit()
    await db.refresh(row)
    return DeviceTokenOut(token=row.token, platform=row.platform, active=row.active)


@router.delete(
    "/{token}", status_code=status.HTTP_204_NO_CONTENT, operation_id="unregister_device_token"
)
async def unregister_device_token(
    token: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Deactivate a device token (logout / uninstall). Only the owner's token is touched."""
    row = await db.scalar(
        select(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user.id)
    )
    if row is not None:
        row.active = False
        await db.commit()
