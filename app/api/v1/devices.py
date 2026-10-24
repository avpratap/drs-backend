"""FCM device-token registration (customer apps register for push)."""
import logging

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.auth import get_current_user
from app.models.device_token import DeviceToken
from app.models.user import User

router = APIRouter(prefix="/account/device-tokens", tags=["devices"])
log = logging.getLogger("drs.devices")


class DeviceTokenIn(BaseModel):
    # Real FCM registration tokens are ~150+ chars; a short min_length let a guessed
    # string be claimed. 64 is comfortably below real tokens yet rejects junk.
    token: str = Field(min_length=64, max_length=512)
    platform: str = Field(default="android", pattern="^(ios|android|web)$")


class DeviceTokenOut(BaseModel):
    token: str
    platform: str
    active: bool


@router.post("", response_model=DeviceTokenOut, operation_id="register_device_token")
async def register_device_token(
    data: DeviceTokenIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Register (or re-activate) this device for push. Idempotent on the token.

    An FCM token is per app-install, not per user: when a different user signs in on
    the same device, re-registering legitimately moves the token to them (which detaches
    the previous owner). We audit that reassignment rather than doing it silently."""
    prev_owner = await db.scalar(select(DeviceToken.user_id).where(DeviceToken.token == data.token))
    if prev_owner is not None and prev_owner != user.id:
        log.info("device token reassigned from user %s to user %s", prev_owner, user.id)
    # Atomic upsert on UNIQUE(token): two concurrent first registrations of the same
    # token (app retry) used to both INSERT and 500 on the unique violation.
    stmt = (
        pg_insert(DeviceToken)
        .values(user_id=user.id, token=data.token, platform=data.platform, active=True)
        .on_conflict_do_update(
            index_elements=[DeviceToken.token],
            set_={"user_id": user.id, "platform": data.platform, "active": True},
        )
        .returning(DeviceToken.token, DeviceToken.platform, DeviceToken.active)
    )
    token, platform, active = (await db.execute(stmt)).one()
    await db.commit()
    return DeviceTokenOut(token=token, platform=platform, active=active)


class DeviceTokenUnregisterIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.post(
    "/unregister", status_code=status.HTTP_204_NO_CONTENT,
    operation_id="unregister_device_token_body",
)
async def unregister_device_token_body(
    data: DeviceTokenUnregisterIn, user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a device token, sent in the BODY (preferred: the DELETE /{token} form
    puts the push token in the URL, i.e. in access logs and traces)."""
    await _deactivate(db, user, data.token)


@router.delete(
    "/{token}", status_code=status.HTTP_204_NO_CONTENT, operation_id="unregister_device_token"
)
async def unregister_device_token(
    token: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """Deactivate a device token (logout / uninstall). Only the owner's token is touched.
    Deprecated: use POST /unregister (keeps the token out of URLs/logs)."""
    await _deactivate(db, user, token)


async def _deactivate(db: AsyncSession, user: User, token: str) -> None:
    row = await db.scalar(
        select(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user.id)
    )
    if row is not None:
        row.active = False
        await db.commit()
