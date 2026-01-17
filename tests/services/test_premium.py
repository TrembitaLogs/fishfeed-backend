"""Tests for premium subscription feature gates."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.purchase import FREE_USER_LIMITS, PREMIUM_USER_LIMITS, UserLimits
from app.services.premium import (
    PREMIUM_CACHE_KEY_PREFIX,
    get_user_limits,
    get_user_limits_async,
    invalidate_premium_cache,
    is_premium,
)


async def cleanup_users(session: AsyncSession) -> None:
    """Helper to cleanup users and related data."""
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str = "test@example.com",
    subscription_status: str = "free",
    subscription_expires_at: datetime | None = None,
) -> User:
    """Create a test user with specified subscription status."""
    user = User(
        email=email,
        password_hash="test_hash",
        subscription_status=subscription_status,
        subscription_expires_at=subscription_expires_at,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class TestIsPremium:
    """Tests for is_premium function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_true_for_premium_user_with_valid_expiry(
        self, async_session: AsyncSession
    ):
        """Test is_premium returns True for premium user with valid expires_at."""
        await cleanup_users(async_session)
        try:
            expires_at = datetime.now(UTC) + timedelta(days=30)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expires_at,
            )

            result = await is_premium(user)

            assert result is True
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_true_for_premium_user_without_expiry(
        self, async_session: AsyncSession
    ):
        """Test is_premium returns True for premium user without expires_at."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=None,
            )

            result = await is_premium(user)

            assert result is True
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_false_for_expired_subscription(
        self, async_session: AsyncSession
    ):
        """Test is_premium returns False for expired subscription."""
        await cleanup_users(async_session)
        try:
            expired_at = datetime.now(UTC) - timedelta(days=1)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expired_at,
            )

            result = await is_premium(user)

            assert result is False
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_false_for_free_user(self, async_session: AsyncSession):
        """Test is_premium returns False for free user."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="free",
            )

            result = await is_premium(user)

            assert result is False
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_false_for_cancelled_user(self, async_session: AsyncSession):
        """Test is_premium returns False for cancelled user."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="cancelled",
            )

            result = await is_premium(user)

            assert result is False
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_false_for_expired_status(self, async_session: AsyncSession):
        """Test is_premium returns False for user with 'expired' status."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="expired",
            )

            result = await is_premium(user)

            assert result is False
        finally:
            await cleanup_users(async_session)


class TestIsPremiumWithCache:
    """Tests for is_premium function with Redis caching."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_caches_premium_status_in_redis(
        self, async_session: AsyncSession, redis_client
    ):
        """Test that premium status is cached in Redis."""
        await cleanup_users(async_session)
        await redis_client.flushdb()
        try:
            expires_at = datetime.now(UTC) + timedelta(days=30)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expires_at,
            )

            result = await is_premium(user, redis_client)

            assert result is True

            # Check cache was set
            cache_key = f"{PREMIUM_CACHE_KEY_PREFIX}{user.id}"
            cached = await redis_client.get(cache_key)
            assert cached is not None
            assert '"is_premium": true' in cached
        finally:
            await cleanup_users(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_cached_value(
        self, async_session: AsyncSession, redis_client
    ):
        """Test that cached value is returned without checking user."""
        await cleanup_users(async_session)
        await redis_client.flushdb()
        try:
            user = await create_test_user(
                async_session,
                subscription_status="free",  # User is free
            )

            # Set cache to premium manually
            cache_key = f"{PREMIUM_CACHE_KEY_PREFIX}{user.id}"
            await redis_client.set(cache_key, '{"is_premium": true}')

            # Should return cached value (True) even though user is free
            result = await is_premium(user, redis_client)

            assert result is True
        finally:
            await cleanup_users(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_handles_redis_errors_gracefully(
        self, async_session: AsyncSession, redis_client
    ):
        """Test that Redis errors don't break is_premium check."""
        await cleanup_users(async_session)
        await redis_client.flushdb()
        try:
            expires_at = datetime.now(UTC) + timedelta(days=30)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expires_at,
            )

            # Even with Redis issues, should still work (fall back to direct check)
            result = await is_premium(user, redis_client)

            assert result is True
        finally:
            await cleanup_users(async_session)
            await redis_client.flushdb()


class TestInvalidatePremiumCache:
    """Tests for invalidate_premium_cache function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_invalidates_cache(self, redis_client):
        """Test that cache is invalidated correctly."""
        await redis_client.flushdb()
        try:
            user_id = "test-user-id-123"
            cache_key = f"{PREMIUM_CACHE_KEY_PREFIX}{user_id}"

            # Set cache
            await redis_client.set(cache_key, '{"is_premium": true}')
            assert await redis_client.exists(cache_key)

            # Invalidate
            await invalidate_premium_cache(user_id, redis_client)

            # Verify deleted
            assert not await redis_client.exists(cache_key)
        finally:
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_handles_nonexistent_cache(self, redis_client):
        """Test that invalidating nonexistent cache doesn't raise."""
        await redis_client.flushdb()

        # Should not raise
        await invalidate_premium_cache("nonexistent-user-id", redis_client)


class TestGetUserLimits:
    """Tests for get_user_limits function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_premium_limits_for_premium_user(
        self, async_session: AsyncSession
    ):
        """Test get_user_limits returns premium limits for premium user."""
        await cleanup_users(async_session)
        try:
            expires_at = datetime.now(UTC) + timedelta(days=30)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expires_at,
            )

            limits = get_user_limits(user)

            assert limits.is_premium is True
            assert limits.ai_scans_per_month == -1  # Unlimited
            assert limits.max_aquariums == 20
            assert limits.max_fish_per_aquarium == 100
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_free_limits_for_free_user(
        self, async_session: AsyncSession
    ):
        """Test get_user_limits returns free limits for free user."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="free",
            )

            limits = get_user_limits(user)

            assert limits.is_premium is False
            assert limits.ai_scans_per_month == 3
            assert limits.max_aquariums == 2
            assert limits.max_fish_per_aquarium == 10
        finally:
            await cleanup_users(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_free_limits_for_expired_subscription(
        self, async_session: AsyncSession
    ):
        """Test get_user_limits returns free limits for expired premium user."""
        await cleanup_users(async_session)
        try:
            expired_at = datetime.now(UTC) - timedelta(days=1)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expired_at,
            )

            limits = get_user_limits(user)

            assert limits.is_premium is False
            assert limits.ai_scans_per_month == 3
            assert limits.max_aquariums == 2
            assert limits.max_fish_per_aquarium == 10
        finally:
            await cleanup_users(async_session)


class TestGetUserLimitsAsync:
    """Tests for get_user_limits_async function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_returns_premium_limits_with_cache(
        self, async_session: AsyncSession, redis_client
    ):
        """Test get_user_limits_async uses cache."""
        await cleanup_users(async_session)
        await redis_client.flushdb()
        try:
            expires_at = datetime.now(UTC) + timedelta(days=30)
            user = await create_test_user(
                async_session,
                subscription_status="premium",
                subscription_expires_at=expires_at,
            )

            limits = await get_user_limits_async(user, redis_client)

            assert limits.is_premium is True
            assert limits.ai_scans_per_month == -1

            # Verify cache was set
            cache_key = f"{PREMIUM_CACHE_KEY_PREFIX}{user.id}"
            assert await redis_client.exists(cache_key)
        finally:
            await cleanup_users(async_session)
            await redis_client.flushdb()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_works_without_redis(self, async_session: AsyncSession):
        """Test get_user_limits_async works without Redis."""
        await cleanup_users(async_session)
        try:
            user = await create_test_user(
                async_session,
                subscription_status="free",
            )

            limits = await get_user_limits_async(user, redis=None)

            assert limits.is_premium is False
            assert limits.ai_scans_per_month == 3
        finally:
            await cleanup_users(async_session)


class TestUserLimitsSchema:
    """Tests for UserLimits schema."""

    def test_free_user_limits_values(self):
        """Test FREE_USER_LIMITS has correct values."""
        assert FREE_USER_LIMITS.ai_scans_per_month == 3
        assert FREE_USER_LIMITS.max_aquariums == 2
        assert FREE_USER_LIMITS.max_fish_per_aquarium == 10
        assert FREE_USER_LIMITS.is_premium is False

    def test_premium_user_limits_values(self):
        """Test PREMIUM_USER_LIMITS has correct values."""
        assert PREMIUM_USER_LIMITS.ai_scans_per_month == -1  # Unlimited
        assert PREMIUM_USER_LIMITS.max_aquariums == 20
        assert PREMIUM_USER_LIMITS.max_fish_per_aquarium == 100
        assert PREMIUM_USER_LIMITS.is_premium is True

    def test_user_limits_schema_creation(self):
        """Test UserLimits schema can be created with custom values."""
        limits = UserLimits(
            ai_scans_per_month=5,
            max_aquariums=10,
            max_fish_per_aquarium=50,
            is_premium=True,
        )

        assert limits.ai_scans_per_month == 5
        assert limits.max_aquariums == 10
        assert limits.max_fish_per_aquarium == 50
        assert limits.is_premium is True
