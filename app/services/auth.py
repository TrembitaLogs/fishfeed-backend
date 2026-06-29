"""Authentication service with business logic for registration, login, and token management."""

import secrets
from typing import Literal
from uuid import UUID

import jwt as pyjwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from jwt import PyJWKClient
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.models.user import User
from app.schemas.auth import TokenResponse, UserResponse
from app.services.email import send_password_reset_email
from app.utils.jwt import create_access_token, create_refresh_token, decode_token
from app.utils.password import hash_password, verify_password

# Apple JWKS endpoint for public keys
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"


class AuthError(AppError):
    """Base class for auth errors. Subclass per concrete failure mode."""


class EmailAlreadyExistsError(AuthError):
    """Raised when email is already registered."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.AUTH_EMAIL_EXISTS, "Email already registered", status_code=409)


class InvalidCredentialsError(AuthError):
    """Raised when login credentials are invalid."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.AUTH_INVALID_CREDENTIALS, "Invalid email or password", status_code=401)


class InvalidRefreshTokenError(AuthError):
    """Raised when refresh token is invalid or expired."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.AUTH_INVALID_REFRESH_TOKEN,
            "Invalid or expired refresh token",
            status_code=401,
        )


class InvalidOAuthTokenError(AuthError):
    """Raised when OAuth token validation fails."""

    def __init__(self, message: str = "Invalid OAuth token") -> None:
        super().__init__(ErrorCode.AUTH_INVALID_OAUTH_TOKEN, message, status_code=401)


class OAuthNotConfiguredError(AuthError):
    """Raised when OAuth provider is not configured."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            ErrorCode.AUTH_OAUTH_NOT_CONFIGURED,
            f"OAuth provider '{provider}' is not configured",
            # A provider that isn't configured/supported is a client-side
            # condition (bad request), not a server fault — must not be 5xx.
            status_code=400,
        )


class InvalidResetTokenError(AuthError):
    """Raised when password reset token is invalid or expired."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.AUTH_INVALID_RESET_TOKEN,
            "Invalid or expired reset token",
            status_code=400,
        )


class OAuthPasswordChangeError(AuthError):
    """Raised when an OAuth user tries to change password."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.AUTH_OAUTH_PASSWORD_CHANGE_DISALLOWED,
            "Cannot change password for OAuth accounts",
            status_code=400,
        )


class InvalidOldPasswordError(AuthError):
    """Raised when old password does not match."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.AUTH_INVALID_OLD_PASSWORD, "Invalid old password", status_code=400)


def _get_refresh_token_ttl() -> int:
    """Get refresh token TTL in seconds."""
    settings = get_settings()
    return settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60


def _redis_key(jti: str) -> str:
    """Build Redis key for refresh token."""
    return f"refresh:{jti}"


def _reset_key(token: str) -> str:
    """Build Redis key for password reset token."""
    return f"password_reset:{token}"


async def request_password_reset(
    db: AsyncSession,
    redis: Redis,
    email: str,
) -> None:
    """Generate a password reset token and send reset email.

    Always succeeds silently to prevent email enumeration.

    Args:
        db: Database session.
        redis: Redis client.
        email: Email address to send reset link to.
    """
    stmt = select(User).where(User.email == email, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        return

    settings = get_settings()
    token = secrets.token_urlsafe(32)
    ttl = settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES * 60

    await redis.set(_reset_key(token), str(user.id), ex=ttl)
    await send_password_reset_email(email, token)


async def confirm_password_reset(
    db: AsyncSession,
    redis: Redis,
    token: str,
    new_password: str,
) -> None:
    """Validate a password reset token and set the new password.

    Args:
        db: Database session.
        redis: Redis client.
        token: Password reset token from email link.
        new_password: New password to set.

    Raises:
        InvalidResetTokenError: If token is invalid or expired.
    """
    key = _reset_key(token)
    stored_user_id = await redis.get(key)
    if stored_user_id is None:
        raise InvalidResetTokenError()

    await redis.delete(key)

    user_id = UUID(stored_user_id)
    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise InvalidResetTokenError()

    user.password_hash = hash_password(new_password)
    user.token_version += 1
    await db.flush()


async def change_password(
    db: AsyncSession,
    user: User,
    old_password: str,
    new_password: str,
) -> None:
    """Change the authenticated user's password.

    Args:
        db: Database session.
        user: Authenticated user object.
        old_password: Current password for verification.
        new_password: New password to set.

    Raises:
        OAuthPasswordChangeError: If user is an OAuth account without a password.
        InvalidOldPasswordError: If old password does not match.
    """
    if user.password_hash is None:
        raise OAuthPasswordChangeError()

    if not verify_password(old_password, user.password_hash):
        raise InvalidOldPasswordError()

    user.password_hash = hash_password(new_password)
    user.token_version += 1
    await db.flush()


async def register_user(
    db: AsyncSession,
    email: str,
    password: str,
) -> User:
    """Register a new user.

    Args:
        db: Database session.
        email: User's email address.
        password: Plain text password.

    Returns:
        Created User object.

    Raises:
        EmailAlreadyExistsError: If email is already registered.
    """
    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    existing_user = result.scalar_one_or_none()

    if existing_user is not None:
        raise EmailAlreadyExistsError()

    user = User(
        email=email,
        password_hash=hash_password(password),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)

    return user


async def login_user(
    db: AsyncSession,
    redis: Redis,
    email: str,
    password: str,
) -> TokenResponse:
    """Authenticate user and generate tokens.

    Args:
        db: Database session.
        redis: Redis client.
        email: User's email address.
        password: Plain text password.

    Returns:
        TokenResponse with access and refresh tokens.

    Raises:
        InvalidCredentialsError: If credentials are invalid.
    """
    stmt = select(User).where(User.email == email, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise InvalidCredentialsError()

    if user.password_hash is None or not verify_password(password, user.password_hash):
        raise InvalidCredentialsError()

    access_token = create_access_token(user.id, token_version=user.token_version)
    refresh_token, jti = create_refresh_token(user.id)

    await redis.set(
        _redis_key(jti),
        str(user.id),
        ex=_get_refresh_token_ttl(),
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
    )


async def refresh_tokens(
    db: AsyncSession,
    redis: Redis,
    refresh_token: str,
) -> TokenResponse:
    """Refresh access token using refresh token.

    Implements token rotation: old refresh token is invalidated
    and a new one is issued.

    Args:
        db: Database session.
        redis: Redis client.
        refresh_token: Current refresh token.

    Returns:
        TokenResponse with new access and refresh tokens.

    Raises:
        InvalidRefreshTokenError: If refresh token is invalid or expired.
    """
    payload = decode_token(refresh_token)
    if payload is None:
        raise InvalidRefreshTokenError()

    if payload.get("type") != "refresh":
        raise InvalidRefreshTokenError()

    jti = payload.get("jti")
    if jti is None:
        raise InvalidRefreshTokenError()

    stored_user_id = await redis.get(_redis_key(jti))
    if stored_user_id is None:
        raise InvalidRefreshTokenError()

    user_id = UUID(payload["sub"])
    if str(user_id) != stored_user_id:
        raise InvalidRefreshTokenError()

    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        await redis.delete(_redis_key(jti))
        raise InvalidRefreshTokenError()

    await redis.delete(_redis_key(jti))

    new_access_token = create_access_token(user.id, token_version=user.token_version)
    new_refresh_token, new_jti = create_refresh_token(user.id)

    await redis.set(
        _redis_key(new_jti),
        str(user.id),
        ex=_get_refresh_token_ttl(),
    )

    return TokenResponse(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        user=UserResponse.model_validate(user),
    )


async def logout(
    redis: Redis,
    refresh_token: str,
) -> None:
    """Invalidate refresh token.

    Args:
        redis: Redis client.
        refresh_token: Refresh token to invalidate.
    """
    payload = decode_token(refresh_token)
    if payload is None:
        return

    jti = payload.get("jti")
    if jti is not None:
        await redis.delete(_redis_key(jti))


def _verify_google_token(token: str) -> dict:
    """Verify Google OAuth ID token.

    Args:
        token: Google ID token.

    Returns:
        Token payload with email and sub.

    Raises:
        InvalidOAuthTokenError: If token validation fails.
        OAuthNotConfiguredError: If Google OAuth is not configured.
    """
    settings = get_settings()
    if not settings.GOOGLE_CLIENT_ID:
        raise OAuthNotConfiguredError("google")

    try:
        request = google_requests.Request()
        id_info = google_id_token.verify_oauth2_token(
            token,
            request,
            audience=settings.GOOGLE_CLIENT_ID,
        )
        return {
            "email": id_info.get("email"),
            "sub": id_info.get("sub"),
        }
    except ValueError as e:
        raise InvalidOAuthTokenError(f"Google token validation failed: {e}") from None


def _verify_apple_token(token: str) -> dict:
    """Verify Apple Sign-In JWT token.

    Args:
        token: Apple identity token (JWT).

    Returns:
        Token payload with email and sub.

    Raises:
        InvalidOAuthTokenError: If token validation fails.
        OAuthNotConfiguredError: If Apple OAuth is not configured.
    """
    settings = get_settings()
    if not settings.APPLE_CLIENT_ID:
        raise OAuthNotConfiguredError("apple")

    try:
        jwks_client = PyJWKClient(APPLE_JWKS_URL, cache_keys=True)
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        payload = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.APPLE_CLIENT_ID,
            issuer=APPLE_ISSUER,
        )
        return {
            "email": payload.get("email"),
            "sub": payload.get("sub"),
        }
    except pyjwt.exceptions.PyJWTError as e:
        raise InvalidOAuthTokenError(f"Apple token validation failed: {e}") from None


async def oauth_login(
    db: AsyncSession,
    redis: Redis,
    provider: Literal["google", "apple"],
    token: str,
) -> TokenResponse:
    """Authenticate user via OAuth provider.

    Validates the OAuth token, creates a new user if not exists,
    and returns access and refresh tokens.

    Args:
        db: Database session.
        redis: Redis client.
        provider: OAuth provider ("google" or "apple").
        token: OAuth ID token from the provider.

    Returns:
        TokenResponse with access and refresh tokens.

    Raises:
        InvalidOAuthTokenError: If OAuth token is invalid.
        OAuthNotConfiguredError: If OAuth provider is not configured.
    """
    if provider == "google":
        token_info = _verify_google_token(token)
    elif provider == "apple":
        token_info = _verify_apple_token(token)
    else:
        raise InvalidOAuthTokenError(f"Unsupported OAuth provider: {provider}")

    email = token_info.get("email")
    oauth_id = token_info.get("sub")

    if not email or not oauth_id:
        raise InvalidOAuthTokenError("OAuth token missing required claims (email, sub)")

    # Check if user exists by oauth_id or email
    stmt = select(User).where(
        or_(
            (User.oauth_provider == provider) & (User.oauth_id == oauth_id),
            User.email == email,
        ),
        User.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        # Create new user
        user = User(
            email=email,
            oauth_provider=provider,
            oauth_id=oauth_id,
            email_verified=True,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)
    elif user.oauth_provider is None:
        # Link OAuth to existing email-based account
        user.oauth_provider = provider
        user.oauth_id = oauth_id
        user.email_verified = True
        await db.flush()
        await db.refresh(user)

    # Generate tokens
    access_token = create_access_token(user.id, token_version=user.token_version)
    refresh_token, jti = create_refresh_token(user.id)

    await redis.set(
        _redis_key(jti),
        str(user.id),
        ex=_get_refresh_token_ttl(),
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
    )
