"""Redis-based rate limiting for AI scan operations.

This module provides hourly rate limiting for free users to protect
against abuse. The total scan limit (5 for free users) is managed
in PostgreSQL, while this Redis-based limiter handles burst protection.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis

from app.config import get_settings


@dataclass
class RateLimitResult:
    """Result of rate limit check.

    Attributes:
        allowed: Whether the request is allowed.
        remaining: Number of requests remaining in current window.
        reset_at: When the rate limit window resets.
        reason: Human-readable reason if not allowed.
    """

    allowed: bool
    remaining: int
    reset_at: datetime
    reason: str | None = None


class AIRateLimiter:
    """Redis-based rate limiter for AI scan operations.

    Implements hourly rate limiting for free users. Premium users
    bypass rate limiting entirely.

    Usage:
        limiter = AIRateLimiter(redis_client)
        result = await limiter.check_scan_limit(user_id, is_premium=False)
        if not result.allowed:
            # Return 429 with Retry-After header
            ...
        # Process scan
        await limiter.increment_scan_count(user_id)
    """

    def __init__(self, redis_client: Redis) -> None:
        """Initialize rate limiter with Redis client.

        Args:
            redis_client: Async Redis client instance.
        """
        self._redis = redis_client
        self._settings = get_settings()

    def _get_rate_key(self, user_id: UUID) -> str:
        """Generate Redis key for user's hourly rate limit.

        Key format: {prefix}ai_scan_rate:{user_id}:{hour}
        The hour component ensures automatic window rotation.

        Args:
            user_id: User identifier.

        Returns:
            Redis key string.
        """
        current_hour = datetime.now(UTC).strftime("%Y%m%d%H")
        prefix = self._settings.REDIS_KEY_PREFIX
        return f"{prefix}ai_scan_rate:{user_id}:{current_hour}"

    def _get_window_reset_time(self) -> datetime:
        """Calculate when the current rate limit window resets.

        Returns:
            Datetime of next hour boundary in UTC.
        """
        now = datetime.now(UTC)
        next_hour = now.replace(minute=0, second=0, microsecond=0)
        # Add one hour
        next_hour = next_hour.replace(hour=next_hour.hour + 1)
        # Handle day rollover
        if next_hour.hour == 0:
            from datetime import timedelta

            next_hour = now.replace(
                minute=0, second=0, microsecond=0
            ) + timedelta(hours=1)
        return next_hour

    async def check_scan_limit(
        self,
        user_id: UUID,
        is_premium: bool,
    ) -> RateLimitResult:
        """Check if user can perform an AI scan.

        Premium users always pass. Free users are limited to
        FREE_USER_HOURLY_SCAN_LIMIT scans per hour.

        Args:
            user_id: User identifier.
            is_premium: Whether user has premium subscription.

        Returns:
            RateLimitResult with allowed status and metadata.
        """
        reset_at = self._get_window_reset_time()

        # Premium users bypass rate limiting
        if is_premium:
            return RateLimitResult(
                allowed=True,
                remaining=-1,  # Unlimited
                reset_at=reset_at,
                reason=None,
            )

        key = self._get_rate_key(user_id)
        hourly_limit = self._settings.FREE_USER_HOURLY_SCAN_LIMIT

        # Get current count
        current_count_str = await self._redis.get(key)
        current_count = int(current_count_str) if current_count_str else 0

        remaining = max(0, hourly_limit - current_count)

        if current_count >= hourly_limit:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_at=reset_at,
                reason="Hourly scan limit exceeded. Please try again later.",
            )

        return RateLimitResult(
            allowed=True,
            remaining=remaining,
            reset_at=reset_at,
            reason=None,
        )

    async def increment_scan_count(self, user_id: UUID) -> int:
        """Increment user's scan count for current hour.

        Should be called after successful scan processing.

        Args:
            user_id: User identifier.

        Returns:
            New count after increment.
        """
        key = self._get_rate_key(user_id)

        # Increment with automatic creation if not exists
        new_count = await self._redis.incr(key)

        # Set TTL on first increment (1 hour = 3600 seconds)
        if new_count == 1:
            await self._redis.expire(key, 3600)

        return int(new_count)

    async def get_current_count(self, user_id: UUID) -> int:
        """Get current scan count for user in this hour.

        Args:
            user_id: User identifier.

        Returns:
            Current count, 0 if no scans yet.
        """
        key = self._get_rate_key(user_id)
        count_str = await self._redis.get(key)
        return int(count_str) if count_str else 0

    async def reset_count(self, user_id: UUID) -> None:
        """Reset user's scan count (for testing purposes).

        Args:
            user_id: User identifier.
        """
        key = self._get_rate_key(user_id)
        await self._redis.delete(key)
