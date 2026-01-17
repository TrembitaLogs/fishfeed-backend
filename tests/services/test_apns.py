"""Tests for APNs service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import PushToken
from app.models.user import User
from app.services.apns import (
    APNsClient,
    APNsConfigError,
    APNsError,
    APNsUnavailableError,
    get_apns_client,
    remove_invalid_tokens,
)
from app.utils.password import hash_password


@pytest.fixture(autouse=True)
def reset_apns_singleton():
    """Reset APNs singleton before and after each test."""
    APNsClient._instance = None
    APNsClient._initialized = False
    APNsClient._client = None
    yield
    APNsClient._instance = None
    APNsClient._initialized = False
    APNsClient._client = None


class TestAPNsError:
    """Tests for APNs error classes."""

    def test_apns_error_base(self):
        """Test base APNs error."""
        error = APNsError("Something went wrong", retriable=True)
        assert error.message == "Something went wrong"
        assert error.retriable is True
        assert str(error) == "Something went wrong"

    def test_apns_config_error(self):
        """Test APNs configuration error."""
        error = APNsConfigError("Missing credentials")
        assert "configuration error" in error.message
        assert "Missing credentials" in error.message
        assert error.retriable is False

    def test_apns_unavailable_error(self):
        """Test APNs unavailable error."""
        error = APNsUnavailableError("Service down")
        assert "unavailable" in error.message
        assert "Service down" in error.message
        assert error.retriable is True

    def test_apns_unavailable_error_without_detail(self):
        """Test APNs unavailable error without detail."""
        error = APNsUnavailableError()
        assert "unavailable" in error.message
        assert error.retriable is True


class TestAPNsClientInitialization:
    """Tests for APNs client initialization."""

    @patch("app.services.apns.get_settings")
    def test_not_configured_when_no_credentials(self, mock_settings):
        """Test APNs not configured when credentials missing."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID=None,
            APNS_TEAM_ID=None,
            APNS_BUNDLE_ID=None,
            APNS_KEY_PATH=None,
            APNS_USE_SANDBOX=True,
        )

        client = APNsClient()
        assert not client.is_configured

    @patch("app.services.apns.get_settings")
    def test_not_configured_when_partial_credentials(self, mock_settings):
        """Test APNs not configured when only some credentials provided."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID=None,
            APNS_BUNDLE_ID="com.example.app",
            APNS_KEY_PATH=None,
            APNS_USE_SANDBOX=True,
        )

        client = APNsClient()
        assert not client.is_configured

    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    def test_initialization_with_valid_credentials(
        self, mock_settings, mock_path, mock_apns
    ):
        """Test APNs initialization with valid credentials."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----"
        mock_path.return_value = mock_path_instance

        client = APNsClient()

        assert client.is_configured
        mock_apns.assert_called_once_with(
            key="-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----",
            key_id="KEYID123",
            team_id="TEAMID123",
            topic="com.example.fishfeed",
            use_sandbox=True,
        )

    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    def test_initialization_production_mode(
        self, mock_settings, mock_path, mock_apns
    ):
        """Test APNs initialization in production mode."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=False,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key-content"
        mock_path.return_value = mock_path_instance

        client = APNsClient()

        assert client.is_configured
        mock_apns.assert_called_once()
        call_kwargs = mock_apns.call_args[1]
        assert call_kwargs["use_sandbox"] is False

    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    def test_singleton_pattern(self, mock_settings, mock_path, mock_apns):
        """Test that APNsClient is a singleton."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        client1 = APNsClient()
        client2 = APNsClient()

        assert client1 is client2
        # APNs should only be initialized once
        assert mock_apns.call_count == 1

    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    def test_initialization_key_file_not_found_raises_error(
        self, mock_settings, mock_path
    ):
        """Test that missing key file raises APNsConfigError."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/nonexistent/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = False
        mock_path.return_value = mock_path_instance

        with pytest.raises(APNsConfigError) as exc_info:
            APNsClient()

        assert "not found" in str(exc_info.value)


class TestAPNsClientSendNotification:
    """Tests for send_notification method."""

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.get_settings")
    async def test_send_when_not_configured_returns_false(self, mock_settings):
        """Test sending when APNs is not configured."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID=None,
            APNS_TEAM_ID=None,
            APNS_BUNDLE_ID=None,
            APNS_KEY_PATH=None,
            APNS_USE_SANDBOX=True,
        )

        client = APNsClient()
        success, error = await client.send_notification(
            token="test-token",
            title="Test",
            body="Test message",
        )

        assert success is False
        assert error == "NOT_CONFIGURED"

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    async def test_send_notification_success(
        self, mock_settings, mock_path, mock_apns
    ):
        """Test successful notification sending."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        mock_result = MagicMock()
        mock_result.is_successful = True
        mock_result.notification_id = "test-notification-id"

        mock_apns_instance = MagicMock()
        mock_apns_instance.send_notification = AsyncMock(return_value=mock_result)
        mock_apns.return_value = mock_apns_instance

        client = APNsClient()
        success, error = await client.send_notification(
            token="test-device-token",
            title="Test Title",
            body="Test Body",
            data={"key": "value"},
            badge=5,
        )

        assert success is True
        assert error is None
        mock_apns_instance.send_notification.assert_called_once()

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    async def test_send_notification_bad_device_token(
        self, mock_settings, mock_path, mock_apns
    ):
        """Test handling of BadDeviceToken error."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        mock_result = MagicMock()
        mock_result.is_successful = False
        mock_result.status = "400"
        mock_result.description = "BadDeviceToken"

        mock_apns_instance = MagicMock()
        mock_apns_instance.send_notification = AsyncMock(return_value=mock_result)
        mock_apns.return_value = mock_apns_instance

        client = APNsClient()
        success, error = await client.send_notification(
            token="invalid-token",
            title="Test",
            body="Test",
        )

        assert success is False
        assert error == "BadDeviceToken"

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    async def test_send_notification_unregistered(
        self, mock_settings, mock_path, mock_apns
    ):
        """Test handling of Unregistered device token."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        mock_result = MagicMock()
        mock_result.is_successful = False
        mock_result.status = "410"
        mock_result.description = "Unregistered"

        mock_apns_instance = MagicMock()
        mock_apns_instance.send_notification = AsyncMock(return_value=mock_result)
        mock_apns.return_value = mock_apns_instance

        client = APNsClient()
        success, error = await client.send_notification(
            token="unregistered-token",
            title="Test",
            body="Test",
        )

        assert success is False
        assert error == "Unregistered"

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.APNS_RESPONSE_CODE")
    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    async def test_send_notification_service_unavailable_raises_error(
        self, mock_settings, mock_path, mock_apns, mock_response_code
    ):
        """Test that service unavailable raises APNsUnavailableError."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        mock_response_code.SERVICE_UNAVAILABLE = "503"

        mock_result = MagicMock()
        mock_result.is_successful = False
        mock_result.status = "503"
        mock_result.description = "Service Unavailable"

        mock_apns_instance = MagicMock()
        mock_apns_instance.send_notification = AsyncMock(return_value=mock_result)
        mock_apns.return_value = mock_apns_instance

        client = APNsClient()

        with pytest.raises(APNsUnavailableError):
            await client.send_notification(
                token="test-token",
                title="Test",
                body="Test",
            )


class TestAPNsClientSendBatch:
    """Tests for send_batch method."""

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.get_settings")
    async def test_batch_when_not_configured_returns_false(self, mock_settings):
        """Test batch sending when APNs is not configured."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID=None,
            APNS_TEAM_ID=None,
            APNS_BUNDLE_ID=None,
            APNS_KEY_PATH=None,
            APNS_USE_SANDBOX=True,
        )

        client = APNsClient()
        results = await client.send_batch(
            tokens=["token1", "token2"],
            title="Test",
            body="Test message",
        )

        assert len(results) == 2
        assert all(not success for success, _ in results)
        assert all(error == "NOT_CONFIGURED" for _, error in results)

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.get_settings")
    async def test_batch_empty_tokens_returns_empty_list(self, mock_settings):
        """Test batch with empty token list."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID=None,
            APNS_TEAM_ID=None,
            APNS_BUNDLE_ID=None,
            APNS_KEY_PATH=None,
            APNS_USE_SANDBOX=True,
        )

        client = APNsClient()
        results = await client.send_batch(tokens=[], title="Test", body="Test")

        assert results == []

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    async def test_batch_success(self, mock_settings, mock_path, mock_apns):
        """Test successful batch sending."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        mock_result = MagicMock()
        mock_result.is_successful = True
        mock_result.notification_id = "test-id"

        mock_apns_instance = MagicMock()
        mock_apns_instance.send_notification = AsyncMock(return_value=mock_result)
        mock_apns.return_value = mock_apns_instance

        client = APNsClient()
        results = await client.send_batch(
            tokens=["token1", "token2", "token3"],
            title="Test",
            body="Test message",
        )

        assert len(results) == 3
        assert all(success for success, _ in results)
        assert all(error is None for _, error in results)

    @pytest.mark.asyncio(loop_scope="session")
    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    async def test_batch_partial_failure(
        self, mock_settings, mock_path, mock_apns
    ):
        """Test batch with partial failures."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        # Create different results for each call
        success_result = MagicMock()
        success_result.is_successful = True
        success_result.notification_id = "success-id"

        failure_result = MagicMock()
        failure_result.is_successful = False
        failure_result.status = "400"
        failure_result.description = "BadDeviceToken"

        mock_apns_instance = MagicMock()
        mock_apns_instance.send_notification = AsyncMock(
            side_effect=[success_result, failure_result, success_result]
        )
        mock_apns.return_value = mock_apns_instance

        client = APNsClient()
        results = await client.send_batch(
            tokens=["valid1", "invalid", "valid2"],
            title="Test",
            body="Test message",
        )

        assert len(results) == 3
        assert results[0] == (True, None)
        assert results[1] == (False, "BadDeviceToken")
        assert results[2] == (True, None)


async def cleanup_push_tokens(session: AsyncSession) -> None:
    """Helper to cleanup push tokens and test users."""
    from sqlalchemy import text

    await session.execute(text("DELETE FROM push_tokens WHERE token LIKE 'test-apns-%'"))
    await session.execute(text("DELETE FROM users WHERE email LIKE 'test-apns-%'"))
    await session.commit()


class TestRemoveInvalidTokens:
    """Tests for remove_invalid_tokens function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_remove_bad_device_tokens(self, async_session: AsyncSession):
        """Test removal of BadDeviceToken from database."""
        await cleanup_push_tokens(async_session)
        try:
            # Create test user
            user = User(
                email=f"test-apns-{uuid.uuid4()}@example.com",
                password_hash=hash_password("testpass123"),
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            # Create push tokens
            tokens = [
                PushToken(
                    user_id=user.id, token="test-apns-valid-token", platform="ios"
                ),
                PushToken(
                    user_id=user.id, token="test-apns-bad-token", platform="ios"
                ),
                PushToken(
                    user_id=user.id, token="test-apns-unregistered-token", platform="ios"
                ),
            ]
            for token in tokens:
                async_session.add(token)
            await async_session.commit()

            # Simulate send results
            results: list[tuple[bool, str | None]] = [
                (True, None),
                (False, "BadDeviceToken"),
                (False, "Unregistered"),
            ]

            removed = await remove_invalid_tokens(
                db=async_session,
                user_id=user.id,
                tokens=[
                    "test-apns-valid-token",
                    "test-apns-bad-token",
                    "test-apns-unregistered-token",
                ],
                results=results,
            )

            # Should remove bad and unregistered tokens
            assert len(removed) == 2
            assert "test-apns-bad-token" in removed
            assert "test-apns-unregistered-token" in removed

            # Verify in database
            stmt = select(PushToken).where(PushToken.user_id == user.id)
            result = await async_session.execute(stmt)
            remaining_tokens = result.scalars().all()

            assert len(remaining_tokens) == 1
            assert remaining_tokens[0].token == "test-apns-valid-token"
        finally:
            await cleanup_push_tokens(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_remove_device_token_not_for_topic(
        self, async_session: AsyncSession
    ):
        """Test removal of DeviceTokenNotForTopic error."""
        await cleanup_push_tokens(async_session)
        try:
            # Create test user
            user = User(
                email=f"test-apns-topic-{uuid.uuid4()}@example.com",
                password_hash=hash_password("testpass123"),
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            # Create push token
            token = PushToken(
                user_id=user.id,
                token="test-apns-wrong-topic-token",
                platform="ios",
            )
            async_session.add(token)
            await async_session.commit()

            # Simulate DeviceTokenNotForTopic error
            results: list[tuple[bool, str | None]] = [(False, "DeviceTokenNotForTopic")]

            removed = await remove_invalid_tokens(
                db=async_session,
                user_id=user.id,
                tokens=["test-apns-wrong-topic-token"],
                results=results,
            )

            assert len(removed) == 1
            assert "test-apns-wrong-topic-token" in removed
        finally:
            await cleanup_push_tokens(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_remove_tokens_no_permanent_errors(
        self, async_session: AsyncSession
    ):
        """Test that transient errors don't remove tokens."""
        await cleanup_push_tokens(async_session)
        try:
            # Create test user
            user = User(
                email=f"test-apns-transient-{uuid.uuid4()}@example.com",
                password_hash=hash_password("testpass123"),
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            # Create push token
            token = PushToken(
                user_id=user.id,
                token="test-apns-temporarily-failed-token",
                platform="ios",
            )
            async_session.add(token)
            await async_session.commit()

            # Simulate transient failure (not a permanent error)
            results: list[tuple[bool, str | None]] = [(False, "TooManyRequests")]

            removed = await remove_invalid_tokens(
                db=async_session,
                user_id=user.id,
                tokens=["test-apns-temporarily-failed-token"],
                results=results,
            )

            # Should not remove token for transient errors
            assert len(removed) == 0

            # Verify token still exists
            stmt = select(PushToken).where(PushToken.user_id == user.id)
            result = await async_session.execute(stmt)
            remaining_tokens = result.scalars().all()

            assert len(remaining_tokens) == 1
        finally:
            await cleanup_push_tokens(async_session)


class TestGetAPNsClient:
    """Tests for get_apns_client function."""

    @patch("app.services.apns.get_settings")
    def test_get_apns_client_returns_singleton(self, mock_settings):
        """Test that get_apns_client returns singleton instance."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID=None,
            APNS_TEAM_ID=None,
            APNS_BUNDLE_ID=None,
            APNS_KEY_PATH=None,
            APNS_USE_SANDBOX=True,
        )

        client1 = get_apns_client()
        client2 = get_apns_client()

        assert client1 is client2


class TestAPNsPayloadBuilding:
    """Tests for APNs payload building."""

    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    def test_payload_with_badge(self, mock_settings, mock_path, mock_apns):
        """Test payload building includes badge when provided."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        client = APNsClient()

        payload = client._build_payload(
            title="Test Title",
            body="Test Body",
            data={"custom": "data"},
            badge=5,
        )

        assert payload["aps"]["alert"]["title"] == "Test Title"
        assert payload["aps"]["alert"]["body"] == "Test Body"
        assert payload["aps"]["badge"] == 5
        assert payload["aps"]["sound"] == "default"
        assert payload["custom"] == "data"

    @patch("app.services.apns.APNs")
    @patch("app.services.apns.Path")
    @patch("app.services.apns.get_settings")
    def test_payload_without_badge(self, mock_settings, mock_path, mock_apns):
        """Test payload building without badge."""
        mock_settings.return_value = MagicMock(
            APNS_KEY_ID="KEYID123",
            APNS_TEAM_ID="TEAMID123",
            APNS_BUNDLE_ID="com.example.fishfeed",
            APNS_KEY_PATH="/path/to/key.p8",
            APNS_USE_SANDBOX=True,
        )

        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.read_text.return_value = "test-key"
        mock_path.return_value = mock_path_instance

        client = APNsClient()

        payload = client._build_payload(
            title="Test Title",
            body="Test Body",
        )

        assert "badge" not in payload["aps"]
        assert payload["aps"]["alert"]["title"] == "Test Title"
        assert payload["aps"]["alert"]["body"] == "Test Body"
