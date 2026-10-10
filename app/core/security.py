"""Auth primitives: Argon2id password hashing + JWT access/refresh tokens.

Full phone-OTP + refresh-rotation/reuse-detection lives in the auth service
(see drs-backend skill); these are the low-level building blocks.
"""
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def create_access_token(sub: str, role: str, token_version: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(sub),
        "role": role,
        "ver": token_version,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)


def generate_refresh_token() -> str:
    """Opaque 256-bit refresh token. Stored server-side only as a hash."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
