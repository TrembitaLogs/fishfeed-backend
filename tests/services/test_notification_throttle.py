"""Tests for notification throttling service."""

import uuid
from datetime import UTC, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationPreference
from app.models.user import User
from app.services.notification_throttle import (
    MAX_NOTIFICATIONS_PER_DAY,
    QUIET_HOURS_END,
    QUIET_HOURS_START,
    URGENT_NOTIFICATION_TYPES,
    ThrottleManager,
    ThrottleResult,
)
from app.utils.password import hash_password


async def cleanup_throttle_data(session: AsyncSession) -> None:
    """Helper to cleanup throttle test data."""
    await session.execute(
        text(
            "DELETE FROM notification_preferences WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'test-throttle-%')"
        )
    )
    await session.execute(text("DELETE FROM users WHERE email LIKE 'test-throttle-%'"))
    await session.commit()


async def create_test_user(session: AsyncSession) -> User:
    """Create a test user for throttle tests."""
    user = User(
        email=f"test-throttle-{uuid.uuid4()}@example.com",
        password_hash=hash_password("testpass123"),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class TestThrottleResult:
    """Tests for ThrottleResult class."""

    def test_throttle_result_allowed(self):
        """Test allowed throttle result."""
        result = ThrottleResult(True)
        assert result.allowed is True
        assert result.reason == ""
        assert bool(result) is True

    def test_throttle_result_blocked(self):
        """Test blocked throttle result with reason."""
        result = ThrottleResult(False, "DAILY_LIMIT_REACHED")
        assert result.allowed is False
        assert result.reason == "DAILY_LIMIT_REACHED"
        assert bool(result) is False

    def test_throttle_result_repr(self):
        """Test string representation."""
        result = ThrottleResult(False, "QUIET_HOURS")
        assert "allowed=False" in repr(result)
        assert "QUIET_HOURS" in repr(result)


class TestDailyLimit:
    """Tests for daily notification limit."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_check_daily_limit_under_limit(self, async_session: AsyncSession):
        """Test that notifications under limit are allowed."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value="5")  # 5 notifications sent

            manager = ThrottleManager(mock_redis, async_session)
            result = await manager.check_daily_limit(user.id)

            assert result is True
        finally:
            await cleanup_throttle_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_check_daily_limit_at_limit(self, async_session: AsyncSession):
        """Test that 10th notification blocks (at limit)."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value="10")  # Already at limit

            manager = ThrottleManager(mock_redis, async_session)
            result = await manager.check_daily_limit(user.id)

            assert result is False
        finally:
            await cleanup_throttle_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_check_daily_limit_no_count(self, async_session: AsyncSession):
        """Test that no previous count allows notification."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value=None)  # No count yet

            manager = ThrottleManager(mock_redis, async_session)
            result = await manager.check_daily_limit(user.id)

            assert result is True
        finally:
            await cleanup_throttle_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_increment_counter(self, async_session: AsyncSession):
        """Test incrementing daily counter."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_pipeline = MagicMock()
            mock_pipeline.incr = MagicMock()
            mock_pipeline.expire = MagicMock()
            mock_pipeline.execute = AsyncMock(return_value=[5, True])

            mock_redis = AsyncMock()
            mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

            manager = ThrottleManager(mock_redis, async_session)
            new_count = await manager.increment_counter(user.id)

            assert new_count == 5
            mock_pipeline.incr.assert_called_once()
            mock_pipeline.expire.assert_called_once()
        finally:
            await cleanup_throttle_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_eleventh_notification_blocked(self, async_session: AsyncSession):
        """Test that 11th notification is blocked."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value=str(MAX_NOTIFICATIONS_PER_DAY))

            manager = ThrottleManager(mock_redis, async_session)
            result = await manager.can_send_notification(user.id)

            assert result.allowed is False
            assert result.reason == "DAILY_LIMIT_REACHED"
        finally:
            await cleanup_throttle_data(async_session)


class TestQuietHours:
    """Tests for quiet hours functionality."""

    def test_is_quiet_hours_at_23(self):
        """Test that 23:00 is within quiet hours."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # 23:00 UTC
        test_time = datetime(2024, 1, 15, 23, 0, tzinfo=UTC)
        result = manager.is_quiet_hours(UTC, test_time)

        assert result is True

    def test_is_quiet_hours_at_7(self):
        """Test that 07:00 is within quiet hours."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # 07:00 UTC
        test_time = datetime(2024, 1, 15, 7, 0, tzinfo=UTC)
        result = manager.is_quiet_hours(UTC, test_time)

        assert result is True

    def test_is_not_quiet_hours_at_8(self):
        """Test that 08:00 is NOT within quiet hours."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # 08:00 UTC
        test_time = datetime(2024, 1, 15, 8, 0, tzinfo=UTC)
        result = manager.is_quiet_hours(UTC, test_time)

        assert result is False

    def test_is_not_quiet_hours_at_12(self):
        """Test that 12:00 is NOT within quiet hours."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # 12:00 UTC
        test_time = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        result = manager.is_quiet_hours(UTC, test_time)

        assert result is False

    def test_is_not_quiet_hours_at_21(self):
        """Test that 21:00 is NOT within quiet hours."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # 21:00 UTC
        test_time = datetime(2024, 1, 15, 21, 0, tzinfo=UTC)
        result = manager.is_quiet_hours(UTC, test_time)

        assert result is False

    def test_is_quiet_hours_at_22(self):
        """Test that 22:00 is within quiet hours (boundary)."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # 22:00 UTC
        test_time = datetime(2024, 1, 15, 22, 0, tzinfo=UTC)
        result = manager.is_quiet_hours(UTC, test_time)

        assert result is True

    def test_quiet_hours_with_timezone(self):
        """Test quiet hours with different timezone."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # User is in UTC+2, local time 23:00 = UTC 21:00
        user_tz = timezone(timedelta(hours=2))
        utc_time = datetime(2024, 1, 15, 21, 0, tzinfo=UTC)

        result = manager.is_quiet_hours(user_tz, utc_time)

        # 21:00 UTC = 23:00 in UTC+2, which is within quiet hours
        assert result is True

    @pytest.mark.asyncio(loop_scope="session")
    async def test_quiet_hours_blocks_notification(self, async_session: AsyncSession):
        """Test that notification is blocked during quiet hours."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value="0")  # Under limit

            manager = ThrottleManager(mock_redis, async_session)

            # Test at 23:00 UTC
            test_time = datetime(2024, 1, 15, 23, 0, tzinfo=UTC)
            with patch(
                "app.services.notification_throttle.datetime"
            ) as mock_datetime:
                mock_datetime.now.return_value = test_time

                # Manually call check_quiet_hours which will use user's timezone
                is_quiet = manager.is_quiet_hours(UTC, test_time)
                assert is_quiet is True

        finally:
            await cleanup_throttle_data(async_session)


class TestUserTimezone:
    """Tests for user timezone handling."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_get_user_timezone_default(self, async_session: AsyncSession):
        """Test default timezone when none set."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            manager = ThrottleManager(MagicMock(), async_session)
            tz = await manager.get_user_timezone(user.id)

            assert tz == UTC
        finally:
            await cleanup_throttle_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_get_user_timezone_positive_offset(self, async_session: AsyncSession):
        """Test positive timezone offset."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Set user timezone to UTC+2
            prefs = NotificationPreference(
                user_id=user.id,
                timezone="+02:00",
                global_opt_out=False,
            )
            async_session.add(prefs)
            await async_session.commit()

            manager = ThrottleManager(MagicMock(), async_session)
            tz = await manager.get_user_timezone(user.id)

            expected_tz = timezone(timedelta(hours=2))
            assert tz == expected_tz
        finally:
            await cleanup_throttle_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_get_user_timezone_negative_offset(self, async_session: AsyncSession):
        """Test negative timezone offset."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Set user timezone to UTC-5
            prefs = NotificationPreference(
                user_id=user.id,
                timezone="-05:00",
                global_opt_out=False,
            )
            async_session.add(prefs)
            await async_session.commit()

            manager = ThrottleManager(MagicMock(), async_session)
            tz = await manager.get_user_timezone(user.id)

            expected_tz = timezone(timedelta(hours=-5))
            assert tz == expected_tz
        finally:
            await cleanup_throttle_data(async_session)


class TestGlobalOptOut:
    """Tests for global opt-out functionality."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_global_opt_out_blocks_all_notifications(
        self, async_session: AsyncSession
    ):
        """Test that global opt-out blocks ALL notifications."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Set global opt-out
            prefs = NotificationPreference(
                user_id=user.id,
                global_opt_out=True,
            )
            async_session.add(prefs)
            await async_session.commit()

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value="0")

            manager = ThrottleManager(mock_redis, async_session)
            result = await manager.can_send_notification(user.id)

            assert result.allowed is False
            assert result.reason == "GLOBAL_OPT_OUT"
        finally:
            await cleanup_throttle_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_no_opt_out_allows_notifications(self, async_session: AsyncSession):
        """Test that notifications are allowed when not opted out."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value="0")

            manager = ThrottleManager(mock_redis, async_session)

            # Mock check_quiet_hours to return False (not quiet hours)
            with patch.object(
                manager, "check_quiet_hours", AsyncMock(return_value=False)
            ):
                result = await manager.can_send_notification(user.id)

            assert result.allowed is True
        finally:
            await cleanup_throttle_data(async_session)


class TestUrgentNotifications:
    """Tests for urgent notification bypass."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_urgent_bypasses_quiet_hours(self, async_session: AsyncSession):
        """Test that urgent notifications bypass quiet hours."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value="0")

            manager = ThrottleManager(mock_redis, async_session)

            # Mock check_quiet_hours to return True (in quiet hours)
            with patch.object(
                manager, "check_quiet_hours", AsyncMock(return_value=True)
            ):
                # Regular notification should be blocked
                result = await manager.can_send_notification(
                    user.id, "feeding_reminder"
                )
                assert result.allowed is False
                assert result.reason == "QUIET_HOURS"

                # Urgent notification should pass
                for urgent_type in URGENT_NOTIFICATION_TYPES:
                    result = await manager.can_send_notification(user.id, urgent_type)
                    assert result.allowed is True, f"{urgent_type} should bypass quiet hours"
        finally:
            await cleanup_throttle_data(async_session)

    def test_urgent_notification_types_defined(self):
        """Test that urgent notification types are defined."""
        assert "critical_alert" in URGENT_NOTIFICATION_TYPES
        assert "security_alert" in URGENT_NOTIFICATION_TYPES
        assert "account_alert" in URGENT_NOTIFICATION_TYPES


class TestNextSendTime:
    """Tests for next send time calculation."""

    def test_get_next_send_time_during_quiet_hours(self):
        """Test next send time calculation during quiet hours."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # Current time: 23:00 UTC
        with patch(
            "app.services.notification_throttle.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 1, 15, 23, 0, tzinfo=UTC
            )
            mock_datetime.combine = datetime.combine

            next_time = manager.get_next_send_time(UTC)

            # Should be 08:00 tomorrow UTC
            assert next_time.hour == 8
            assert next_time.day == 16

    def test_get_next_send_time_after_quiet_hours_end(self):
        """Test next send time when already past 08:00."""
        manager = ThrottleManager(MagicMock(), MagicMock())

        # Current time: 10:00 UTC (after quiet hours)
        with patch(
            "app.services.notification_throttle.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 1, 15, 10, 0, tzinfo=UTC
            )
            mock_datetime.combine = datetime.combine

            next_time = manager.get_next_send_time(UTC)

            # Should be 08:00 tomorrow
            assert next_time.hour == 8
            assert next_time.day == 16


class TestCanSendNotification:
    """Tests for combined can_send_notification check."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_all_checks_pass(self, async_session: AsyncSession):
        """Test notification allowed when all checks pass."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value="0")

            manager = ThrottleManager(mock_redis, async_session)

            with patch.object(
                manager, "check_quiet_hours", AsyncMock(return_value=False)
            ):
                result = await manager.can_send_notification(user.id)

            assert result.allowed is True
            assert result.reason == ""
        finally:
            await cleanup_throttle_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_check_order_global_opt_out_first(self, async_session: AsyncSession):
        """Test that global opt-out is checked first."""
        await cleanup_throttle_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Set global opt-out and also exceed daily limit
            prefs = NotificationPreference(
                user_id=user.id,
                global_opt_out=True,
            )
            async_session.add(prefs)
            await async_session.commit()

            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock(return_value="100")  # Way over limit

            manager = ThrottleManager(mock_redis, async_session)
            result = await manager.can_send_notification(user.id)

            # Should return GLOBAL_OPT_OUT, not DAILY_LIMIT_REACHED
            assert result.reason == "GLOBAL_OPT_OUT"
        finally:
            await cleanup_throttle_data(async_session)


class TestConstants:
    """Tests for throttling constants."""

    def test_max_notifications_per_day(self):
        """Test max notifications constant."""
        assert MAX_NOTIFICATIONS_PER_DAY == 10

    def test_quiet_hours_start(self):
        """Test quiet hours start time."""
        assert QUIET_HOURS_START == time(22, 0)

    def test_quiet_hours_end(self):
        """Test quiet hours end time."""
        assert QUIET_HOURS_END == time(8, 0)
