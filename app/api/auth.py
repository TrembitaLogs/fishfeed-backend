"""Authentication API endpoints."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.middleware.rate_limit import RateLimiter, _get_client_ip, _hash_ip
from app.redis import get_redis
from app.schemas.auth import (
    LoginRequest,
    OAuthRequest,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from app.services.auth import (
    change_password,
    confirm_password_reset,
    login_user,
    logout,
    oauth_login,
    refresh_tokens,
    register_user,
    request_password_reset,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


async def _check_auth_rate_limit(
    http_request: Request,
    redis: Redis,
    limit: int,
    key_type: str,
) -> None:
    """Check per-IP rate limit for auth endpoints.

    Args:
        http_request: The incoming FastAPI request.
        redis: Async Redis client.
        limit: Max requests per window.
        key_type: Rate limit key type (e.g. 'auth_login', 'auth_register').

    Raises:
        AppError: 429 if rate limit exceeded.
    """
    settings = get_settings()
    client_ip = _get_client_ip(http_request)
    ip_hash = _hash_ip(client_ip, settings.ANALYTICS_IP_SALT)

    limiter = RateLimiter(redis)
    result = await limiter.check_rate_limit(ip_hash, key_type, limit)

    if not result.allowed:
        raise AppError(
            code=ErrorCode.RATE_LIMITED,
            message="Too many requests. Please try again later.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(result.retry_after or 60)},
        )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    responses={
        201: {"description": "User registered successfully"},
        409: {"description": "Email already exists"},
        422: {"description": "Validation error"},
    },
)
async def register(
    request: RegisterRequest,
    http_request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> TokenResponse:
    """Register a new user account.

    Creates a new user with the provided email and password,
    then returns access and refresh tokens.
    """
    settings = get_settings()
    await _check_auth_rate_limit(http_request, redis, settings.RATE_LIMIT_REGISTER_PER_MIN, "auth_register")

    await register_user(db, request.email, request.password)
    return await login_user(db, redis, request.email, request.password)


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Login user",
    responses={
        200: {"description": "Login successful"},
        401: {"description": "Invalid credentials"},
    },
)
async def login(
    request: LoginRequest,
    http_request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> TokenResponse:
    """Authenticate user with email and password.

    Returns access and refresh tokens on successful authentication.
    """
    settings = get_settings()
    await _check_auth_rate_limit(http_request, redis, settings.RATE_LIMIT_LOGIN_PER_MIN, "auth_login")

    return await login_user(db, redis, request.email, request.password)


@router.post(
    "/oauth",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="OAuth login",
    responses={
        200: {"description": "OAuth login successful"},
        400: {"description": "OAuth provider not configured"},
        401: {"description": "Invalid OAuth token"},
    },
)
async def oauth(
    request: OAuthRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> TokenResponse:
    """Authenticate user via OAuth provider (Google or Apple).

    Validates the OAuth token, creates a new user if not exists,
    and returns access and refresh tokens.
    """
    return await oauth_login(db, redis, request.provider, request.token)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Refresh access token",
    responses={
        200: {"description": "Tokens refreshed successfully"},
        401: {"description": "Invalid or expired refresh token"},
    },
)
async def refresh(
    request: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> TokenResponse:
    """Refresh access token using a valid refresh token.

    Implements token rotation: the old refresh token is invalidated
    and a new pair of tokens is issued.
    """
    return await refresh_tokens(db, redis, request.refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Logout user",
    responses={
        204: {"description": "Logout successful"},
        401: {"description": "Not authenticated"},
    },
)
async def logout_user(
    request: RefreshRequest,
    redis: Annotated[Redis, Depends(get_redis)],
    current_user: CurrentActiveUser,
) -> None:
    """Logout user by invalidating the refresh token.

    Requires authentication. The provided refresh token will be
    removed from the token store.
    """
    await logout(redis, request.refresh_token)


@router.post(
    "/password/reset",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request password reset",
    responses={
        202: {"description": "Password reset email sent (if email exists)"},
    },
)
async def password_reset(
    request: PasswordResetRequest,
    http_request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, str]:
    """Request a password reset email.

    Sends a password reset link to the provided email address
    if a user with that email exists. Always returns success
    to prevent email enumeration.
    """
    settings = get_settings()
    await _check_auth_rate_limit(http_request, redis, settings.RATE_LIMIT_LOGIN_PER_MIN, "auth_password_reset")

    await request_password_reset(db, redis, request.email)

    return {"message": "If the email exists, a password reset link has been sent"}


@router.post(
    "/password/reset/confirm",
    status_code=status.HTTP_200_OK,
    summary="Confirm password reset",
    responses={
        200: {"description": "Password reset successfully"},
        400: {"description": "Invalid or expired reset token"},
    },
)
async def password_reset_confirm(
    request: PasswordResetConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, str]:
    """Complete a password reset using the token from the reset email.

    Validates the reset token and sets the new password.
    The token is single-use and expires after use.
    """
    await confirm_password_reset(db, redis, request.token, request.new_password)

    return {"message": "Password has been reset successfully"}


@router.post(
    "/password/change",
    status_code=status.HTTP_200_OK,
    summary="Change password",
    responses={
        200: {"description": "Password changed successfully"},
        400: {"description": "Invalid old password"},
        401: {"description": "Not authenticated"},
    },
)
async def password_change(
    request: PasswordChangeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> dict[str, str]:
    """Change the authenticated user's password.

    Requires the current password for verification before
    setting the new password.
    """
    await change_password(db, current_user, request.old_password, request.new_password)

    return {"message": "Password changed successfully"}


@router.delete(
    "/account",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete account",
    responses={
        204: {"description": "Account deleted successfully"},
        401: {"description": "Not authenticated"},
    },
)
async def delete_account(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    current_user: CurrentActiveUser,
) -> None:
    """Soft delete the authenticated user's account.

    Sets the deleted_at timestamp on the user record.
    The account data is preserved but the user can no longer login.
    """
    current_user.deleted_at = datetime.now(UTC)
    current_user.token_version += 1
    await db.flush()
