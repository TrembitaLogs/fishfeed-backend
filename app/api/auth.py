"""Authentication API endpoints."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.models.user import User
from app.redis import get_redis
from app.schemas.auth import (
    LoginRequest,
    OAuthRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from app.services.auth import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidOAuthTokenError,
    InvalidRefreshTokenError,
    OAuthNotConfiguredError,
    login_user,
    logout,
    oauth_login,
    refresh_tokens,
    register_user,
)
from app.utils.password import hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["Authentication"])


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
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> TokenResponse:
    """Register a new user account.

    Creates a new user with the provided email and password,
    then returns access and refresh tokens.
    """
    try:
        await register_user(db, request.email, request.password)
        return await login_user(db, redis, request.email, request.password)
    except EmailAlreadyExistsError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


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
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> TokenResponse:
    """Authenticate user with email and password.

    Returns access and refresh tokens on successful authentication.
    """
    try:
        return await login_user(db, redis, request.email, request.password)
    except InvalidCredentialsError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.post(
    "/oauth",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="OAuth login",
    responses={
        200: {"description": "OAuth login successful"},
        401: {"description": "Invalid OAuth token"},
        500: {"description": "OAuth provider not configured"},
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
    try:
        return await oauth_login(db, redis, request.provider, request.token)
    except (InvalidOAuthTokenError, OAuthNotConfiguredError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


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
    try:
        return await refresh_tokens(db, redis, request.refresh_token)
    except InvalidRefreshTokenError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


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
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Request a password reset email.

    Sends a password reset link to the provided email address
    if a user with that email exists. Always returns success
    to prevent email enumeration.
    """
    # Check if user exists (but don't reveal this to the client)
    stmt = select(User).where(User.email == request.email, User.deleted_at.is_(None))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is not None:
        # TODO: Generate reset token and send email
        # This will be implemented when email service is available
        pass

    return {"message": "If the email exists, a password reset link has been sent"}


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
    if current_user.password_hash is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change password for OAuth accounts",
        )

    if not verify_password(request.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid old password",
        )

    current_user.password_hash = hash_password(request.new_password)
    await db.commit()

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
    await db.commit()
