"""Token issuance + refresh rotation with reuse detection."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token, generate_refresh_token, hash_token
from app.models.auth_session import AuthSession
from app.models.user import User, UserRole


class AuthError(Exception):
    pass


async def get_or_create_user(db: AsyncSession, phone: str) -> User:
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(phone=phone, phone_verified=True, role=UserRole.customer.value)
        db.add(user)
        await db.flush()
    elif not user.phone_verified:
        user.phone_verified = True
    return user


async def issue_tokens(
    db: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    device_name: str | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, str, AuthSession]:
    raw_refresh = generate_refresh_token()
    session = AuthSession(
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        refresh_hash=hash_token(raw_refresh),
        device_name=device_name,
        user_agent=user_agent,
        ip=ip,
        expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_DAYS),
    )
    db.add(session)
    await db.flush()
    access = create_access_token(sub=str(user.id), role=user.role, token_version=user.token_version)
    return access, raw_refresh, session


async def rotate_refresh(
    db: AsyncSession,
    raw_refresh: str,
    *,
    device_name: str | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[User, str, str]:
    result = await db.execute(
        select(AuthSession).where(AuthSession.refresh_hash == hash_token(raw_refresh))
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise AuthError("invalid_refresh_token")

    if session.revoked:
        # Reuse of an already-rotated token => theft. Kill the whole family.
        await db.execute(
            update(AuthSession)
            .where(AuthSession.family_id == session.family_id)
            .values(revoked=True)
        )
        raise AuthError("refresh_token_reuse_detected")

    if session.expires_at <= datetime.now(UTC):
        raise AuthError("refresh_token_expired")

    user = await db.get(User, session.user_id)
    if user is None or user.status != "active":
        raise AuthError("inactive_user")

    access, new_raw, new_session = await issue_tokens(
        db, user, family_id=session.family_id, device_name=device_name, user_agent=user_agent, ip=ip
    )
    session.revoked = True
    session.replaced_by = new_session.id
    return user, access, new_raw


async def revoke_family_by_refresh(db: AsyncSession, raw_refresh: str) -> None:
    result = await db.execute(
        select(AuthSession).where(AuthSession.refresh_hash == hash_token(raw_refresh))
    )
    session = result.scalar_one_or_none()
    if session is not None:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.family_id == session.family_id)
            .values(revoked=True)
        )
