"""Tests for Redis-based AI scan rate limiting."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.services.rate_limiter import AIRateLimiter, RateLimitResult


async def cleanup_rate_limit_keys(redis: Redis) -> None:
    """Helper to cleanup rate limit keys."""
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="fishfeed:ai_scan_rate:*")
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break


@pytest.mark.asyncio(loop_scope="session")
async def test_premium_user_always_allowed(redis_client: Redis):
    """Test that premium users always pass rate limit check."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        result = await limiter.check_scan_limit(user_id, is_premium=True)

        assert result.allowed is True
        assert result.remaining == -1  # Unlimited
        assert result.reason is None
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_free_user_allowed_within_limit(redis_client: Redis):
    """Test that free users are allowed within hourly limit."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        result = await limiter.check_scan_limit(user_id, is_premium=False)

        assert result.allowed is True
        assert result.remaining == 10  # Default limit
        assert result.reason is None
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_free_user_blocked_when_limit_exceeded(redis_client: Redis):
    """Test that free users are blocked when hourly limit exceeded."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        # Simulate 10 scans (hit the limit)
        for _ in range(10):
            await limiter.increment_scan_count(user_id)

        result = await limiter.check_scan_limit(user_id, is_premium=False)

        assert result.allowed is False
        assert result.remaining == 0
        assert result.reason is not None
        assert "limit exceeded" in result.reason.lower()
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_increment_scan_count_increases_counter(redis_client: Redis):
    """Test that increment_scan_count properly increases the counter."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        count1 = await limiter.increment_scan_count(user_id)
        count2 = await limiter.increment_scan_count(user_id)
        count3 = await limiter.increment_scan_count(user_id)

        assert count1 == 1
        assert count2 == 2
        assert count3 == 3

        current = await limiter.get_current_count(user_id)
        assert current == 3
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_remaining_decreases_with_usage(redis_client: Redis):
    """Test that remaining count decreases as user scans."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        # Check initial
        result1 = await limiter.check_scan_limit(user_id, is_premium=False)
        assert result1.remaining == 10

        # Increment 3 times
        await limiter.increment_scan_count(user_id)
        await limiter.increment_scan_count(user_id)
        await limiter.increment_scan_count(user_id)

        # Check again
        result2 = await limiter.check_scan_limit(user_id, is_premium=False)
        assert result2.remaining == 7
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_redis_key_has_ttl(redis_client: Redis):
    """Test that rate limit keys have TTL set (1 hour)."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        await limiter.increment_scan_count(user_id)

        key = limiter._get_rate_key(user_id)
        ttl = await redis_client.ttl(key)

        # TTL should be set and less than or equal to 3600 seconds (1 hour)
        assert ttl > 0
        assert ttl <= 3600
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_reset_at_is_next_hour(redis_client: Redis):
    """Test that reset_at points to the next hour boundary."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        result = await limiter.check_scan_limit(user_id, is_premium=False)

        now = datetime.now(UTC)
        # reset_at should be within the next hour
        assert result.reset_at > now
        assert result.reset_at <= now + timedelta(hours=1)
        # reset_at should be at minute 0
        assert result.reset_at.minute == 0
        assert result.reset_at.second == 0
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_reset_count_clears_counter(redis_client: Redis):
    """Test that reset_count clears the user's counter."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        # Add some scans
        await limiter.increment_scan_count(user_id)
        await limiter.increment_scan_count(user_id)
        assert await limiter.get_current_count(user_id) == 2

        # Reset
        await limiter.reset_count(user_id)

        # Should be 0 now
        assert await limiter.get_current_count(user_id) == 0
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_different_users_have_separate_limits(redis_client: Redis):
    """Test that different users have independent rate limits."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user1 = uuid4()
        user2 = uuid4()

        # User 1 uses 5 scans
        for _ in range(5):
            await limiter.increment_scan_count(user1)

        # User 2 uses 2 scans
        await limiter.increment_scan_count(user2)
        await limiter.increment_scan_count(user2)

        # Check limits
        result1 = await limiter.check_scan_limit(user1, is_premium=False)
        result2 = await limiter.check_scan_limit(user2, is_premium=False)

        assert result1.remaining == 5
        assert result2.remaining == 8
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_current_count_returns_zero_for_new_user(redis_client: Redis):
    """Test that get_current_count returns 0 for users with no scans."""
    await cleanup_rate_limit_keys(redis_client)
    try:
        limiter = AIRateLimiter(redis_client)
        user_id = uuid4()

        count = await limiter.get_current_count(user_id)

        assert count == 0
    finally:
        await cleanup_rate_limit_keys(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_rate_limit_result_dataclass():
    """Test RateLimitResult dataclass initialization."""
    reset_at = datetime.now(UTC) + timedelta(hours=1)

    result = RateLimitResult(
        allowed=True,
        remaining=5,
        reset_at=reset_at,
        reason=None,
    )

    assert result.allowed is True
    assert result.remaining == 5
    assert result.reset_at == reset_at
    assert result.reason is None

    result_blocked = RateLimitResult(
        allowed=False,
        remaining=0,
        reset_at=reset_at,
        reason="Limit exceeded",
    )

    assert result_blocked.allowed is False
    assert result_blocked.reason == "Limit exceeded"
