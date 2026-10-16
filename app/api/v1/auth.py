from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.phone import normalize_phone
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
        "ip": request.client.host if request.client else None,
    }


@router.post("/otp/request", response_model=OTPRequestResponse, operation_id="request_otp")
async def request_otp(data: OTPRequest, request: Request):
    ip = request.client.host if request.client else None
    try:
        dev_otp = await otp_service.request_otp(normalize_phone(data.phone), ip=ip)
    except ValueError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    return OTPRequestResponse(detail="OTP sent", dev_otp=dev_otp)


@router.post("/otp/verify", response_model=TokenPair, operation_id="verify_otp")
async def verify_otp(data: OTPVerify, request: Request, db: AsyncSession = Depends(get_db)):
    phone = normalize_phone(data.phone)
    if not await otp_service.verify_otp(phone, data.otp):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired OTP")
    user = await auth_service.get_or_create_user(db, phone)
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
