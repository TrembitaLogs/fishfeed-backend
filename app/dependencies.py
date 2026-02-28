"""FastAPI dependencies for authentication and authorization."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordBearer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.user import User
from app.redis import get_redis
from app.services.premium import is_premium as check_is_premium
from app.services.rate_limiter import AIRateLimiter, RateLimitResult
from app.utils.jwt import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Validate JWT access token and return the authenticated user.

    Args:
        token: JWT access token from Authorization header.
        db: Database session.

    Returns:
        Authenticated User object.

    Raises:
        HTTPException: 401 if token is missing, invalid, expired, or user not found.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_token(token)

    if payload is None:
        raise credentials_exception

    if payload.get("type") != "access":
        raise credentials_exception

    try:
        user_id = UUID(payload["sub"])
    except (KeyError, ValueError):
        raise credentials_exception from None

    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception

    # Validate token version to ensure token was not invalidated by password change
    token_version = payload.get("tv", 0)
    if token_version != user.token_version:
        raise credentials_exception

    return user


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Verify that the authenticated user account is active.

    Args:
        current_user: User from get_current_user dependency.

    Returns:
        Active User object.

    Raises:
        HTTPException: 403 if user account is deactivated (soft deleted).
    """
    if current_user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated",
        )

    return current_user


async def require_admin(
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> User:
    """Verify that the authenticated user is an admin.

    Args:
        current_user: Active user from get_current_active_user dependency.

    Returns:
        Admin User object.

    Raises:
        HTTPException: 403 if user is not an admin.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )

    return current_user


async def require_premium(
    current_user: Annotated[User, Depends(get_current_active_user)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> User:
    """Verify that the authenticated user has an active premium subscription.

    Uses cached premium status check for performance.

    Args:
        current_user: Active user from get_current_active_user dependency.
        redis: Redis client for caching premium status.

    Returns:
        Premium User object.

    Raises:
        HTTPException: 403 if user does not have premium subscription.
    """
    if not await check_is_premium(current_user, redis):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Premium subscription required",
        )

    return current_user


async def check_ai_scan_rate_limit(
    current_user: Annotated[User, Depends(get_current_active_user)],
    redis: Annotated[Redis, Depends(get_redis)],
    response: Response,
) -> RateLimitResult:
    """Check AI scan rate limit before processing.

    This dependency enforces hourly rate limiting for free users
    to protect against abuse. Premium users bypass rate limiting.

    Args:
        current_user: Active authenticated user.
        redis: Redis client for rate limit tracking.
        response: FastAPI response for adding rate limit headers.

    Returns:
        RateLimitResult with rate limit status.

    Raises:
        HTTPException: 429 Too Many Requests if rate limit exceeded.
    """
    is_premium = current_user.subscription_status != "free"
    limiter = AIRateLimiter(redis)

    result = await limiter.check_scan_limit(current_user.id, is_premium)

    # Add rate limit headers to response
    if not is_premium:
        response.headers["X-RateLimit-Limit"] = str(
            limiter._settings.FREE_USER_HOURLY_SCAN_LIMIT
        )
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(int(result.reset_at.timestamp()))

    if not result.allowed:
        now = datetime.now(UTC)
        retry_after = int((result.reset_at - now).total_seconds())
        retry_after = max(1, retry_after)  # Ensure at least 1 second
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=result.reason or "Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    return result


async def check_image_upload_rate_limit(
    current_user: Annotated[User, Depends(get_current_active_user)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """Check image upload rate limit before processing.

    Enforces per-user rate limiting for image uploads (20/min by default).
    Unlike AI scan rate limit, this applies equally to all users
    (no premium bypass).

    Args:
        current_user: Active authenticated user.
        redis: Redis client for rate limit tracking.

    Raises:
        HTTPException: 429 Too Many Requests if rate limit exceeded.
    """
    settings = get_settings()
    key = f"{settings.REDIS_KEY_PREFIX}image_upload:{current_user.id}"

    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, 60)
    results = await pipe.execute()
    current = results[0]

    if current > settings.RATE_LIMIT_IMAGE_UPLOAD_PER_MIN:
        ttl = await redis.ttl(key)
        retry_after = max(1, ttl)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Image upload rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )


# Type aliases for cleaner endpoint signatures
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActiveUser = Annotated[User, Depends(get_current_active_user)]
CurrentAdmin = Annotated[User, Depends(require_admin)]
CurrentPremiumUser = Annotated[User, Depends(require_premium)]
RateLimitCheck = Annotated[RateLimitResult, Depends(check_ai_scan_rate_limit)]
ImageUploadRateLimitCheck = Annotated[None, Depends(check_image_upload_rate_limit)]
