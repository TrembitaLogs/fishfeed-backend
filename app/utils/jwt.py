"""JWT token utilities for access and refresh token management."""

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from jwt import ExpiredSignatureError, PyJWTError

from app.config import get_settings


def create_access_token(
    user_id: UUID,
    token_version: int = 0,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT access token.

    Args:
        user_id: User's unique identifier.
        token_version: User's token version for invalidation on password change.
        expires_delta: Optional custom expiration time.

    Returns:
        Encoded JWT access token string.
    """
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)

    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "exp": now + expires_delta,
        "iat": now,
        "type": "access",
        "tv": token_version,
    }
    return str(jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM))


def create_refresh_token(
    user_id: UUID,
    expires_delta: timedelta | None = None,
) -> tuple[str, str]:
    """Create a JWT refresh token.

    Args:
        user_id: User's unique identifier.
        expires_delta: Optional custom expiration time.

    Returns:
        Tuple of (encoded JWT refresh token string, jti string).
    """
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)

    now = datetime.now(UTC)
    jti = str(uuid.uuid4())
    payload = {
        "sub": str(user_id),
        "exp": now + expires_delta,
        "iat": now,
        "type": "refresh",
        "jti": jti,
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, jti


def decode_token(token: str) -> dict | None:
    """Decode and validate a JWT token.

    Args:
        token: JWT token string to decode.

    Returns:
        Token payload dict if valid, None if expired or invalid.
    """
    settings = get_settings()
    try:
        payload: dict[str, str | int] = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload
    except ExpiredSignatureError:
        return None
    except PyJWTError:
        return None
