from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clientip import client_ip
from app.core.db import get_db
from app.core.phone import is_indian_mobile, normalize_phone
from app.deps.auth import get_current_user
from app.models.user import User
from app.schemas.auth import (
    OTPRequest,
    OTPRequestResponse,
    OTPVerify,
    RefreshRequest,
    TokenPair,
    UserOut,
)
from app.schemas.common import DetailOut
from app.services import auth as auth_service
from app.services import otp as otp_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_meta(request: Request) -> dict:
    return {
        "device_name": request.headers.get("user-agent", "")[:255] or None,
        "user_agent": request.headers.get("user-agent", "")[:512] or None,
        "ip": client_ip(request),
    }


# exclude_none: in prod (or once an SMS was sent) the response carries NO dev_otp key at
# all, not `"dev_otp": null` — a client testing for the key's presence must never show a
# "dev code" box in production.
@router.post(
    "/otp/request",
    response_model=OTPRequestResponse,
    response_model_exclude_none=True,
    operation_id="request_otp",
)
async def request_otp(data: OTPRequest, request: Request):
    phone = normalize_phone(data.phone)
    if not is_indian_mobile(phone):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Indian mobile number")
    ip = client_ip(request)
    try:
        dev_otp = await otp_service.request_otp(phone, ip=ip)
    except otp_service.UseLatestCode:
        # Capped, but the last code sent to this number is still valid: not an error.
        return OTPRequestResponse(
            detail="An OTP was already sent to this number; use the latest code you received"
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    return OTPRequestResponse(detail="OTP sent", dev_otp=dev_otp)


@router.post("/otp/verify", response_model=TokenPair, operation_id="verify_otp")
async def verify_otp(data: OTPVerify, request: Request, db: AsyncSession = Depends(get_db)):
    phone = normalize_phone(data.phone)
    if not is_indian_mobile(phone):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Indian mobile number")
    try:
        ok = await otp_service.verify_otp(phone, data.otp, ip=client_ip(request))
    except ValueError as exc:  # per-IP failed-verify cap
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired OTP")
    user = await auth_service.get_or_create_user(db, phone)
    if user.status != "active":
        # Don't mint (unusable) tokens + a session row for a suspended/erased account.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is not active")
    access, refresh, _ = await auth_service.issue_tokens(db, user, **_client_meta(request))
    await db.commit()
    return TokenPair(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenPair, operation_id="refresh_token")
async def refresh_token(data: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        _user, access, refresh = await auth_service.rotate_refresh(
            db, data.refresh_token, **_client_meta(request)
        )
    except auth_service.AuthError as exc:
        await db.commit()  # persist family revocation if reuse was detected
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    await db.commit()
    return TokenPair(access_token=access, refresh_token=refresh)


@router.post("/logout", operation_id="logout", response_model=DetailOut)
async def logout(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    await auth_service.revoke_family_by_refresh(db, data.refresh_token)
    await db.commit()
    return {"detail": "logged out"}


@router.get("/me", response_model=UserOut, operation_id="get_me")
async def me(user: User = Depends(get_current_user)):
    return user
