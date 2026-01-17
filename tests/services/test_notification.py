"""Tests for unified notification service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    NotificationLog,
    NotificationPreference,
    PushToken,
)
from app.models.user import User
from app.services.notification import (
    DEFAULT_PREFERENCES,
    NOTIFICATION_TYPE_TO_PREFERENCE,
    NotificationService,
    get_notification_service,
)
from app.utils.password import hash_password


async def cleanup_notification_data(session: AsyncSession) -> None:
    """Helper to cleanup notification test data."""
    await session.execute(
        text("DELETE FROM notification_logs WHERE title LIKE 'Test%'")
    )
    await session.execute(text("DELETE FROM push_tokens WHERE token LIKE 'test-%'"))
    await session.execute(
        text(
            "DELETE FROM notification_preferences WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'test-notif-%')"
        )
    )
    await session.execute(text("DELETE FROM users WHERE email LIKE 'test-notif-%'"))
    await session.commit()


async def create_test_user(session: AsyncSession) -> User:
    """Create a test user for notification tests."""
    user = User(
        email=f"test-notif-{uuid.uuid4()}@example.com",
        password_hash=hash_password("testpass123"),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class TestNotificationTypeMapping:
    """Tests for notification type to preference mapping."""

    def test_all_notification_types_mapped(self):
        """Test that all expected notification types are mapped."""
        expected_types = {
            "feeding_reminder",
            "overdue_alert",
            "streak_protection",
            "weekly_summary",
            "family_update",
            "marketing",
        }
        assert set(NOTIFICATION_TYPE_TO_PREFERENCE.keys()) == expected_types

    def test_default_preferences_match_model(self):
        """Test that default preferences match model fields."""
        expected_fields = {
            "global_opt_out",
            "timezone",
            "feeding_reminders",
            "overdue_alerts",
            "streak_protection",
            "weekly_summary",
            "family_updates",
            "marketing",
        }
        assert set(DEFAULT_PREFERENCES.keys()) == expected_fields


class TestRegisterPushToken:
    """Tests for register_push_token method."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_register_new_token(self, async_session: AsyncSession):
        """Test registering a new push token."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            await service.register_push_token(
                user_id=user.id,
                token="test-ios-token-123",
                platform="ios",
            )

            # Verify token was saved
            stmt = select(PushToken).where(
                PushToken.user_id == user.id,
                PushToken.token == "test-ios-token-123",
            )
            result = await async_session.execute(stmt)
            token = result.scalar_one_or_none()

            assert token is not None
            assert token.platform == "ios"
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_register_multiple_tokens_for_user(self, async_session: AsyncSession):
        """Test that a user can have multiple tokens (different devices)."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            await service.register_push_token(
                user_id=user.id,
                token="test-ios-token-device1",
                platform="ios",
            )
            await service.register_push_token(
                user_id=user.id,
                token="test-android-token-device2",
                platform="android",
            )

            # Verify both tokens exist
            stmt = select(PushToken).where(PushToken.user_id == user.id)
            result = await async_session.execute(stmt)
            tokens = result.scalars().all()

            assert len(tokens) == 2
            platforms = {t.platform for t in tokens}
            assert platforms == {"ios", "android"}
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_register_existing_token_updates_platform(
        self, async_session: AsyncSession
    ):
        """Test that re-registering same token updates platform."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            # Register as iOS first
            await service.register_push_token(
                user_id=user.id,
                token="test-shared-token",
                platform="ios",
            )

            # Re-register as Android
            await service.register_push_token(
                user_id=user.id,
                token="test-shared-token",
                platform="android",
            )

            # Should only have one token with updated platform
            stmt = select(PushToken).where(
                PushToken.user_id == user.id,
                PushToken.token == "test-shared-token",
            )
            result = await async_session.execute(stmt)
            tokens = result.scalars().all()

            assert len(tokens) == 1
            assert tokens[0].platform == "android"
        finally:
            await cleanup_notification_data(async_session)


class TestUnregisterPushToken:
    """Tests for unregister_push_token method."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_unregister_existing_token(self, async_session: AsyncSession):
        """Test unregistering an existing token."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            # Register token first
            await service.register_push_token(
                user_id=user.id,
                token="test-token-to-remove",
                platform="ios",
            )

            # Unregister
            result = await service.unregister_push_token(
                user_id=user.id,
                token="test-token-to-remove",
            )

            assert result is True

            # Verify removed
            stmt = select(PushToken).where(
                PushToken.user_id == user.id,
                PushToken.token == "test-token-to-remove",
            )
            db_result = await async_session.execute(stmt)
            token = db_result.scalar_one_or_none()

            assert token is None
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_unregister_nonexistent_token(self, async_session: AsyncSession):
        """Test unregistering a token that doesn't exist."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            result = await service.unregister_push_token(
                user_id=user.id,
                token="test-nonexistent-token",
            )

            assert result is False
        finally:
            await cleanup_notification_data(async_session)


class TestGetUserPreferences:
    """Tests for get_user_preferences method."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_get_default_preferences_when_none_set(
        self, async_session: AsyncSession
    ):
        """Test getting default preferences when none are set."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            preferences = await service.get_user_preferences(user.id)

            assert preferences == DEFAULT_PREFERENCES
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_get_existing_preferences(self, async_session: AsyncSession):
        """Test getting existing preferences."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            # Create custom preferences
            prefs = NotificationPreference(
                user_id=user.id,
                feeding_reminders=False,
                overdue_alerts=True,
                streak_protection=False,
                weekly_summary=True,
                family_updates=False,
                marketing=True,
            )
            async_session.add(prefs)
            await async_session.commit()

            preferences = await service.get_user_preferences(user.id)

            assert preferences["feeding_reminders"] is False
            assert preferences["overdue_alerts"] is True
            assert preferences["streak_protection"] is False
            assert preferences["weekly_summary"] is True
            assert preferences["family_updates"] is False
            assert preferences["marketing"] is True
        finally:
            await cleanup_notification_data(async_session)


class TestUpdatePreferences:
    """Tests for update_preferences method."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_update_creates_preferences_if_none_exist(
        self, async_session: AsyncSession
    ):
        """Test that updating creates preferences if none exist."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            result = await service.update_preferences(
                user_id=user.id,
                prefs={"marketing": True},
            )

            assert result["marketing"] is True
            # Other values should be defaults
            assert result["feeding_reminders"] is True
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_update_existing_preferences(self, async_session: AsyncSession):
        """Test updating existing preferences."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            # Create initial preferences
            prefs = NotificationPreference(
                user_id=user.id,
                feeding_reminders=True,
                overdue_alerts=True,
                streak_protection=True,
                weekly_summary=True,
                family_updates=True,
                marketing=False,
            )
            async_session.add(prefs)
            await async_session.commit()

            # Update some preferences
            result = await service.update_preferences(
                user_id=user.id,
                prefs={"feeding_reminders": False, "marketing": True},
            )

            assert result["feeding_reminders"] is False
            assert result["marketing"] is True
            # Unchanged values
            assert result["overdue_alerts"] is True
        finally:
            await cleanup_notification_data(async_session)


class TestIsNotificationAllowed:
    """Tests for _is_notification_allowed method."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_none_notification_type_always_allowed(
        self, async_session: AsyncSession
    ):
        """Test that None notification type is always allowed."""
        service = NotificationService(async_session)
        preferences = {"feeding_reminders": False}
        assert service._is_notification_allowed(preferences, None) is True

    @pytest.mark.asyncio(loop_scope="session")
    async def test_unknown_notification_type_allowed_by_default(
        self, async_session: AsyncSession
    ):
        """Test that unknown notification types are allowed by default."""
        service = NotificationService(async_session)
        preferences = {}
        assert service._is_notification_allowed(preferences, "unknown_type") is True

    @pytest.mark.asyncio(loop_scope="session")
    async def test_disabled_preference_blocks_notification(
        self, async_session: AsyncSession
    ):
        """Test that disabled preference blocks notification."""
        service = NotificationService(async_session)
        preferences = {"feeding_reminders": False}
        assert (
            service._is_notification_allowed(preferences, "feeding_reminder") is False
        )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_enabled_preference_allows_notification(
        self, async_session: AsyncSession
    ):
        """Test that enabled preference allows notification."""
        service = NotificationService(async_session)
        preferences = {"marketing": True}
        assert service._is_notification_allowed(preferences, "marketing") is True


class TestSendPush:
    """Tests for send_push method."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_send_push_no_tokens_returns_false(self, async_session: AsyncSession):
        """Test that send_push returns False when user has no tokens."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            result = await service.send_push(
                user_id=user.id,
                title="Test Title",
                body="Test Body",
            )

            assert result is False
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_send_push_disabled_preference_returns_false(
        self, async_session: AsyncSession
    ):
        """Test that disabled preference returns False and logs."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)
            service = NotificationService(async_session)

            # Register a token
            await service.register_push_token(
                user_id=user.id,
                token="test-token-pref-disabled",
                platform="ios",
            )

            # Disable feeding reminders
            await service.update_preferences(
                user_id=user.id,
                prefs={"feeding_reminders": False},
            )

            result = await service.send_push(
                user_id=user.id,
                title="Test Feeding Reminder",
                body="Time to feed!",
                notification_type="feeding_reminder",
            )

            assert result is False

            # Verify log was created
            stmt = select(NotificationLog).where(
                NotificationLog.user_id == user.id,
                NotificationLog.error_code == "PREFERENCE_DISABLED",
            )
            db_result = await async_session.execute(stmt)
            log = db_result.scalar_one_or_none()

            assert log is not None
            assert log.success is False
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.notification.get_apns_client")
    @patch("app.services.notification.get_fcm_client")
    async def test_send_push_routes_to_ios(
        self,
        mock_get_fcm,
        mock_get_apns,
        async_session: AsyncSession,
    ):
        """Test that iOS tokens are routed to APNs."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Setup mocks
            mock_apns = AsyncMock()
            mock_apns.is_configured = True
            mock_apns.send_batch = AsyncMock(return_value=[(True, None)])
            mock_get_apns.return_value = mock_apns

            mock_fcm = MagicMock()
            mock_fcm.is_configured = False
            mock_get_fcm.return_value = mock_fcm

            service = NotificationService(async_session)

            # Register iOS token
            await service.register_push_token(
                user_id=user.id,
                token="test-ios-route-token",
                platform="ios",
            )

            result = await service.send_push(
                user_id=user.id,
                title="Test iOS",
                body="Test Body",
            )

            assert result is True
            mock_apns.send_batch.assert_called_once()
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.notification.get_apns_client")
    @patch("app.services.notification.get_fcm_client")
    async def test_send_push_routes_to_android(
        self,
        mock_get_fcm,
        mock_get_apns,
        async_session: AsyncSession,
    ):
        """Test that Android tokens are routed to FCM."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Setup mocks
            mock_apns = AsyncMock()
            mock_apns.is_configured = False
            mock_get_apns.return_value = mock_apns

            mock_fcm = MagicMock()
            mock_fcm.is_configured = True
            mock_fcm.send_multicast = MagicMock(return_value=[(True, None)])
            mock_get_fcm.return_value = mock_fcm

            service = NotificationService(async_session)

            # Register Android token
            await service.register_push_token(
                user_id=user.id,
                token="test-android-route-token",
                platform="android",
            )

            result = await service.send_push(
                user_id=user.id,
                title="Test Android",
                body="Test Body",
            )

            assert result is True
            mock_fcm.send_multicast.assert_called_once()
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.notification.get_apns_client")
    @patch("app.services.notification.get_fcm_client")
    async def test_send_push_to_both_platforms(
        self,
        mock_get_fcm,
        mock_get_apns,
        async_session: AsyncSession,
    ):
        """Test sending to user with both iOS and Android tokens."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Setup mocks
            mock_apns = AsyncMock()
            mock_apns.is_configured = True
            mock_apns.send_batch = AsyncMock(return_value=[(True, None)])
            mock_get_apns.return_value = mock_apns

            mock_fcm = MagicMock()
            mock_fcm.is_configured = True
            mock_fcm.send_multicast = MagicMock(return_value=[(True, None)])
            mock_get_fcm.return_value = mock_fcm

            service = NotificationService(async_session)

            # Register both iOS and Android tokens
            await service.register_push_token(
                user_id=user.id,
                token="test-ios-both-token",
                platform="ios",
            )
            await service.register_push_token(
                user_id=user.id,
                token="test-android-both-token",
                platform="android",
            )

            result = await service.send_push(
                user_id=user.id,
                title="Test Both Platforms",
                body="Test Body",
            )

            assert result is True
            mock_apns.send_batch.assert_called_once()
            mock_fcm.send_multicast.assert_called_once()
        finally:
            await cleanup_notification_data(async_session)


class TestSendPushBatch:
    """Tests for send_push_batch method."""

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.notification.get_apns_client")
    @patch("app.services.notification.get_fcm_client")
    async def test_send_push_batch_to_multiple_users(
        self,
        mock_get_fcm,
        mock_get_apns,
        async_session: AsyncSession,
    ):
        """Test sending batch notifications to multiple users."""
        await cleanup_notification_data(async_session)
        try:
            # Create multiple test users
            users = []
            for i in range(3):
                user = User(
                    email=f"test-notif-batch-{i}-{uuid.uuid4()}@example.com",
                    password_hash=hash_password("testpass123"),
                )
                async_session.add(user)
                users.append(user)
            await async_session.commit()
            for user in users:
                await async_session.refresh(user)

            # Setup mocks
            mock_apns = AsyncMock()
            mock_apns.is_configured = True
            mock_apns.send_batch = AsyncMock(return_value=[(True, None)])
            mock_get_apns.return_value = mock_apns

            mock_fcm = MagicMock()
            mock_fcm.is_configured = False
            mock_get_fcm.return_value = mock_fcm

            service = NotificationService(async_session)

            # Register tokens for each user
            for i, user in enumerate(users):
                await service.register_push_token(
                    user_id=user.id,
                    token=f"test-batch-token-{i}",
                    platform="ios",
                )

            # Send batch
            results = await service.send_push_batch(
                user_ids=[u.id for u in users],
                title="Test Batch",
                body="Batch notification",
            )

            assert len(results) == 3
            assert all(results)  # All should succeed
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.notification.get_apns_client")
    @patch("app.services.notification.get_fcm_client")
    async def test_send_push_batch_partial_success(
        self,
        mock_get_fcm,
        mock_get_apns,
        async_session: AsyncSession,
    ):
        """Test batch with some users having no tokens."""
        await cleanup_notification_data(async_session)
        try:
            # Create users - some with tokens, some without
            users = []
            for i in range(2):
                user = User(
                    email=f"test-notif-partial-{i}-{uuid.uuid4()}@example.com",
                    password_hash=hash_password("testpass123"),
                )
                async_session.add(user)
                users.append(user)
            await async_session.commit()
            for user in users:
                await async_session.refresh(user)

            # Setup mocks
            mock_apns = AsyncMock()
            mock_apns.is_configured = True
            mock_apns.send_batch = AsyncMock(return_value=[(True, None)])
            mock_get_apns.return_value = mock_apns

            mock_fcm = MagicMock()
            mock_fcm.is_configured = False
            mock_get_fcm.return_value = mock_fcm

            service = NotificationService(async_session)

            # Only register token for first user
            await service.register_push_token(
                user_id=users[0].id,
                token="test-partial-token",
                platform="ios",
            )

            # Send batch - second user has no tokens
            results = await service.send_push_batch(
                user_ids=[u.id for u in users],
                title="Test Partial",
                body="Partial notification",
            )

            # First user should succeed, second should fail (no tokens)
            assert results[0] is True
            assert results[1] is False
        finally:
            await cleanup_notification_data(async_session)


class TestNotificationLogging:
    """Tests for notification logging."""

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.notification.get_apns_client")
    @patch("app.services.notification.get_fcm_client")
    async def test_successful_notification_logged(
        self,
        mock_get_fcm,
        mock_get_apns,
        async_session: AsyncSession,
    ):
        """Test that successful notifications are logged."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Setup mocks
            mock_apns = AsyncMock()
            mock_apns.is_configured = True
            mock_apns.send_batch = AsyncMock(return_value=[(True, None)])
            mock_get_apns.return_value = mock_apns

            mock_fcm = MagicMock()
            mock_fcm.is_configured = False
            mock_get_fcm.return_value = mock_fcm

            service = NotificationService(async_session)

            # Register token
            await service.register_push_token(
                user_id=user.id,
                token="test-log-success-token",
                platform="ios",
            )

            await service.send_push(
                user_id=user.id,
                title="Test Log Success",
                body="This should be logged",
                notification_type="weekly_summary",
            )

            # Verify log was created
            stmt = select(NotificationLog).where(
                NotificationLog.user_id == user.id,
                NotificationLog.title == "Test Log Success",
            )
            result = await async_session.execute(stmt)
            log = result.scalar_one_or_none()

            assert log is not None
            assert log.success is True
            assert log.notification_type == "weekly_summary"
            assert log.platform == "ios"
            assert log.error_code is None
        finally:
            await cleanup_notification_data(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.notification.get_apns_client")
    @patch("app.services.notification.get_fcm_client")
    async def test_failed_notification_logged_with_error(
        self,
        mock_get_fcm,
        mock_get_apns,
        async_session: AsyncSession,
    ):
        """Test that failed notifications are logged with error code."""
        await cleanup_notification_data(async_session)
        try:
            user = await create_test_user(async_session)

            # Setup mocks
            mock_apns = AsyncMock()
            mock_apns.is_configured = True
            mock_apns.send_batch = AsyncMock(return_value=[(False, "BadDeviceToken")])
            mock_get_apns.return_value = mock_apns

            mock_fcm = MagicMock()
            mock_fcm.is_configured = False
            mock_get_fcm.return_value = mock_fcm

            service = NotificationService(async_session)

            # Register token
            await service.register_push_token(
                user_id=user.id,
                token="test-log-failure-token",
                platform="ios",
            )

            await service.send_push(
                user_id=user.id,
                title="Test Log Failure",
                body="This should fail",
            )

            # Verify log was created with error
            stmt = select(NotificationLog).where(
                NotificationLog.user_id == user.id,
                NotificationLog.title == "Test Log Failure",
            )
            result = await async_session.execute(stmt)
            log = result.scalar_one_or_none()

            assert log is not None
            assert log.success is False
            assert log.error_code == "BadDeviceToken"
        finally:
            await cleanup_notification_data(async_session)


class TestGetNotificationService:
    """Tests for get_notification_service function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_get_notification_service(self, async_session: AsyncSession):
        """Test getting notification service instance."""
        service = await get_notification_service(async_session)

        assert isinstance(service, NotificationService)
        assert service.db is async_session
