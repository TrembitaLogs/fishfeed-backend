"""Premium subscription feature gates and user limits management."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import ParamSpec, TypeVar

from fastapi import HTTPException, status
from redis.asyncio import Redis

from app.models.user import User
from app.schemas.purchase import (
    FREE_USER_LIMITS,
    PREMIUM_USER_LIMITS,
    UserLimits,
)

logger = logging.getLogger(__name__)

# Redis cache settings
PREMIUM_CACHE_TTL_SECONDS = 300  # 5 minutes
PREMIUM_CACHE_KEY_PREFIX = "premium_status:"

# Type variables for decorator
P = ParamSpec("P")
R = TypeVar("R")


def _is_subscription_active(user: User) -> bool:
    """Check if user has an active premium subscription.

    Args:
        user: User model instance.

    Returns:
        True if user has premium status with valid expiry (or no expiry set).
    """
    if user.subscription_status != "premium":
        return False

    # If no expiry date, subscription is active
    if user.subscription_expires_at is None:
        return True

    # Check if subscription has expired
    now = datetime.now(UTC)
    expires_at = user.subscription_expires_at

    # Ensure timezone awareness
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    return expires_at > now


async def is_premium(user: User, redis: Redis | None = None) -> bool:
    """Check if user has an active premium subscription.

    Checks subscription_status and expires_at with optional Redis caching.
    Cache is stored for 5 minutes to reduce database lookups.

    Args:
        user: User model instance.
        redis: Optional Redis client for caching.

    Returns:
        True if user has active premium subscription.
    """
    user_id = str(user.id)

    # Try to get from cache if Redis is available
    if redis is not None:
        cache_key = f"{PREMIUM_CACHE_KEY_PREFIX}{user_id}"
        try:
            cached = await redis.get(cache_key)
            if cached is not None:
                cached_data = json.loads(cached)
                logger.debug(f"Premium status cache hit for user {user_id}")
                return bool(cached_data.get("is_premium", False))
        except Exception as e:
            logger.warning(f"Redis cache read error for premium status: {e}")

    # Check subscription status
    is_active = _is_subscription_active(user)

    # Cache the result if Redis is available
    if redis is not None:
        cache_key = f"{PREMIUM_CACHE_KEY_PREFIX}{user_id}"
        try:
            await redis.set(
                cache_key,
                json.dumps({"is_premium": is_active}),
                ex=PREMIUM_CACHE_TTL_SECONDS,
            )
            logger.debug(f"Cached premium status for user {user_id}: {is_active}")
        except Exception as e:
            logger.warning(f"Redis cache write error for premium status: {e}")

    return is_active


async def invalidate_premium_cache(user_id: str, redis: Redis) -> None:
    """Invalidate premium status cache for a user.

    Should be called when subscription status changes.

    Args:
        user_id: User UUID as string.
        redis: Redis client.
    """
    cache_key = f"{PREMIUM_CACHE_KEY_PREFIX}{user_id}"
    try:
        await redis.delete(cache_key)
        logger.debug(f"Invalidated premium cache for user {user_id}")
    except Exception as e:
        logger.warning(f"Redis cache invalidation error: {e}")


def get_user_limits(user: User) -> UserLimits:
    """Get feature limits for a user based on their subscription tier.

    Args:
        user: User model instance.

    Returns:
        UserLimits with appropriate limits for user's subscription tier.
    """
    if _is_subscription_active(user):
        return PREMIUM_USER_LIMITS
    return FREE_USER_LIMITS


async def get_user_limits_async(user: User, redis: Redis | None = None) -> UserLimits:
    """Get feature limits for a user with cached premium check.

    Args:
        user: User model instance.
        redis: Optional Redis client for caching premium status.

    Returns:
        UserLimits with appropriate limits for user's subscription tier.
    """
    if await is_premium(user, redis):
        return PREMIUM_USER_LIMITS
    return FREE_USER_LIMITS


def premium_required(
    feature_name: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator to require premium subscription for route handlers.

    This decorator checks if the current_user has an active premium subscription.
    It expects the decorated function to have a 'current_user' parameter.

    Args:
        feature_name: Optional feature name for error message customization.

    Returns:
        Decorator function.

    Example:
        @router.get("/premium-feature")
        @premium_required(feature_name="advanced analytics")
        async def get_advanced_analytics(current_user: CurrentActiveUser):
            ...
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # Extract current_user from kwargs
            current_user = kwargs.get("current_user")

            if current_user is None:
                # Try to find in args (less common with FastAPI)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="current_user not found in request context",
                )

            if not _is_subscription_active(current_user):  # type: ignore[arg-type]
                detail = "Premium subscription required"
                if feature_name:
                    detail = f"Premium subscription required for {feature_name}"
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=detail,
                )

            return await func(*args, **kwargs)  # type: ignore[no-any-return, misc]

        return wrapper  # type: ignore[return-value]

    return decorator
