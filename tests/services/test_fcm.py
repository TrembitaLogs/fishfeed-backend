"""Tests for FCM service."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from firebase_admin import exceptions, messaging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import PushToken
from app.models.user import User
from app.services.fcm import (
    FCMClient,
    FCMConfigError,
    FCMError,
    FCMUnavailableError,
    get_fcm_client,
    remove_invalid_tokens,
)
from app.utils.password import hash_password


@pytest.fixture(autouse=True)
def reset_fcm_singleton():
    """Reset FCM singleton before and after each test."""
    FCMClient._instance = None
    FCMClient._initialized = False
    yield
    FCMClient._instance = None
    FCMClient._initialized = False


class TestFCMError:
    """Tests for FCM error classes."""

    def test_fcm_error_base(self):
        """Test base FCM error."""
        error = FCMError("Something went wrong", retriable=True)
        assert error.message == "Something went wrong"
        assert error.retriable is True
        assert str(error) == "Something went wrong"

    def test_fcm_config_error(self):
        """Test FCM configuration error."""
        error = FCMConfigError("Missing credentials")
        assert "configuration error" in error.message
        assert "Missing credentials" in error.message
        assert error.retriable is False

    def test_fcm_unavailable_error(self):
        """Test FCM unavailable error."""
        error = FCMUnavailableError("Service down")
        assert "unavailable" in error.message
        assert "Service down" in error.message
        assert error.retriable is True

    def test_fcm_unavailable_error_without_detail(self):
        """Test FCM unavailable error without detail."""
        error = FCMUnavailableError()
        assert "unavailable" in error.message
        assert error.retriable is True


class TestFCMClientInitialization:
    """Tests for FCM client initialization."""

    @patch("app.services.fcm.get_settings")
    def test_not_configured_when_no_credentials(self, mock_settings):
        """Test FCM not configured when credentials missing."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID=None,
            FCM_CREDENTIALS_PATH=None,
            GOOGLE_APPLICATION_CREDENTIALS=None,
        )

        client = FCMClient()
        assert not client.is_configured

    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_initialization_with_credentials_path(
        self, mock_settings, mock_certificate, mock_firebase_admin
    ):
        """Test FCM initialization with credentials path."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/credentials.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
        )
        mock_firebase_admin._apps = {}

        client = FCMClient()
        assert client.is_configured
        mock_certificate.assert_called_once_with("/path/to/credentials.json")
        mock_firebase_admin.initialize_app.assert_called_once()

    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_initialization_with_google_application_credentials(
        self, mock_settings, mock_certificate, mock_firebase_admin
    ):
        """Test FCM initialization with GOOGLE_APPLICATION_CREDENTIALS."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH=None,
            GOOGLE_APPLICATION_CREDENTIALS="/path/to/google_creds.json",
        )
        mock_firebase_admin._apps = {}

        client = FCMClient()
        assert client.is_configured
        mock_certificate.assert_called_once_with("/path/to/google_creds.json")

    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_singleton_pattern(
        self, mock_settings, mock_certificate, mock_firebase_admin
    ):
        """Test that FCMClient is a singleton."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/credentials.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
        )
        mock_firebase_admin._apps = {}

        client1 = FCMClient()
        client2 = FCMClient()

        assert client1 is client2
        # initialize_app should only be called once
        assert mock_firebase_admin.initialize_app.call_count == 1

    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_initialization_error_raises_config_error(
        self, mock_settings, mock_certificate
    ):
        """Test that initialization errors raise FCMConfigError."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/invalid/path.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
        )
        mock_certificate.side_effect = FileNotFoundError("File not found")

        with pytest.raises(FCMConfigError) as exc_info:
            FCMClient()

        assert "File not found" in str(exc_info.value)


class TestFCMClientSendNotification:
    """Tests for send_notification method."""

    @patch("app.services.fcm.get_settings")
    def test_send_when_not_configured_returns_false(self, mock_settings):
        """Test sending when FCM is not configured."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID=None,
            FCM_CREDENTIALS_PATH=None,
            GOOGLE_APPLICATION_CREDENTIALS=None,
        )

        client = FCMClient()
        success, error = client.send_notification(
            token="test-token",
            title="Test",
            body="Test message",
        )

        assert success is False
        assert error == "NOT_CONFIGURED"

    @patch("app.services.fcm.messaging.send")
    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_send_notification_success(
        self, mock_settings, mock_certificate, mock_firebase_admin, mock_send
    ):
        """Test successful notification sending."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/creds.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
            FCM_DRY_RUN=False,
        )
        mock_firebase_admin._apps = {}
        mock_send.return_value = "projects/test/messages/123"

        client = FCMClient()
        success, error = client.send_notification(
            token="test-token",
            title="Test Title",
            body="Test Body",
            data={"key": "value"},
        )

        assert success is True
        assert error is None
        mock_send.assert_called_once()

    @patch("app.services.fcm.messaging.send")
    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_send_notification_unregistered_error(
        self, mock_settings, mock_certificate, mock_firebase_admin, mock_send
    ):
        """Test handling of unregistered token error."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/creds.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
            FCM_DRY_RUN=False,
        )
        mock_firebase_admin._apps = {}
        mock_send.side_effect = messaging.UnregisteredError("Token not registered")

        client = FCMClient()
        success, error = client.send_notification(
            token="invalid-token",
            title="Test",
            body="Test",
        )

        assert success is False
        assert error == "UNREGISTERED"

    @patch("app.services.fcm.messaging.send")
    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_send_notification_invalid_argument_error(
        self, mock_settings, mock_certificate, mock_firebase_admin, mock_send
    ):
        """Test handling of invalid argument error."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/creds.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
            FCM_DRY_RUN=False,
        )
        mock_firebase_admin._apps = {}
        mock_send.side_effect = exceptions.InvalidArgumentError("Invalid token format")

        client = FCMClient()
        success, error = client.send_notification(
            token="malformed-token",
            title="Test",
            body="Test",
        )

        assert success is False
        assert error == "INVALID_ARGUMENT"

    @patch("app.services.fcm.messaging.send")
    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_send_notification_unavailable_raises_error(
        self, mock_settings, mock_certificate, mock_firebase_admin, mock_send
    ):
        """Test that unavailable error raises FCMUnavailableError."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/creds.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
            FCM_DRY_RUN=False,
        )
        mock_firebase_admin._apps = {}
        mock_send.side_effect = exceptions.UnavailableError("Service down")

        client = FCMClient()

        with pytest.raises(FCMUnavailableError):
            client.send_notification(token="test-token", title="Test", body="Test")

    @patch("app.services.fcm.messaging.send")
    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_send_notification_timeout_raises_error(
        self, mock_settings, mock_certificate, mock_firebase_admin, mock_send
    ):
        """Test that timeout error raises FCMUnavailableError."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/creds.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
            FCM_DRY_RUN=False,
        )
        mock_firebase_admin._apps = {}
        mock_send.side_effect = exceptions.DeadlineExceededError("Timeout")

        client = FCMClient()

        with pytest.raises(FCMUnavailableError):
            client.send_notification(token="test-token", title="Test", body="Test")


class TestFCMClientSendMulticast:
    """Tests for send_multicast method."""

    @patch("app.services.fcm.get_settings")
    def test_multicast_when_not_configured_returns_false(self, mock_settings):
        """Test multicast when FCM is not configured."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID=None,
            FCM_CREDENTIALS_PATH=None,
            GOOGLE_APPLICATION_CREDENTIALS=None,
        )

        client = FCMClient()
        results = client.send_multicast(
            tokens=["token1", "token2"],
            title="Test",
            body="Test message",
        )

        assert len(results) == 2
        assert all(not success for success, _ in results)
        assert all(error == "NOT_CONFIGURED" for _, error in results)

    @patch("app.services.fcm.get_settings")
    def test_multicast_empty_tokens_returns_empty_list(self, mock_settings):
        """Test multicast with empty token list."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID=None,
            FCM_CREDENTIALS_PATH=None,
            GOOGLE_APPLICATION_CREDENTIALS=None,
        )

        client = FCMClient()
        results = client.send_multicast(tokens=[], title="Test", body="Test")

        assert results == []

    @patch("app.services.fcm.messaging.send_each_for_multicast")
    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_multicast_success(
        self, mock_settings, mock_certificate, mock_firebase_admin, mock_send_multicast
    ):
        """Test successful multicast sending."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/creds.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
            FCM_DRY_RUN=False,
        )
        mock_firebase_admin._apps = {}

        # Mock response with all successes
        mock_response = MagicMock()
        mock_response.success_count = 3
        mock_response.failure_count = 0
        mock_response.responses = [
            MagicMock(success=True, exception=None),
            MagicMock(success=True, exception=None),
            MagicMock(success=True, exception=None),
        ]
        mock_send_multicast.return_value = mock_response

        client = FCMClient()
        results = client.send_multicast(
            tokens=["token1", "token2", "token3"],
            title="Test",
            body="Test message",
        )

        assert len(results) == 3
        assert all(success for success, _ in results)
        assert all(error is None for _, error in results)

    @patch("app.services.fcm.messaging.send_each_for_multicast")
    @patch("app.services.fcm.firebase_admin")
    @patch("app.services.fcm.credentials.Certificate")
    @patch("app.services.fcm.get_settings")
    def test_multicast_partial_failure(
        self, mock_settings, mock_certificate, mock_firebase_admin, mock_send_multicast
    ):
        """Test multicast with partial failures."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID="test-project",
            FCM_CREDENTIALS_PATH="/path/to/creds.json",
            GOOGLE_APPLICATION_CREDENTIALS=None,
            FCM_DRY_RUN=False,
        )
        mock_firebase_admin._apps = {}

        # Mock response with mixed results
        mock_response = MagicMock()
        mock_response.success_count = 2
        mock_response.failure_count = 1
        mock_response.responses = [
            MagicMock(success=True, exception=None),
            MagicMock(
                success=False,
                exception=messaging.UnregisteredError("Unregistered"),
            ),
            MagicMock(success=True, exception=None),
        ]
        mock_send_multicast.return_value = mock_response

        client = FCMClient()
        results = client.send_multicast(
            tokens=["valid1", "invalid", "valid2"],
            title="Test",
            body="Test message",
        )

        assert len(results) == 3
        assert results[0] == (True, None)
        assert results[1] == (False, "UNREGISTERED")
        assert results[2] == (True, None)


async def cleanup_push_tokens(session: AsyncSession) -> None:
    """Helper to cleanup push tokens and test users."""
    from sqlalchemy import text

    await session.execute(text("DELETE FROM push_tokens WHERE token LIKE 'test-%'"))
    await session.execute(text("DELETE FROM users WHERE email LIKE 'test-fcm-%'"))
    await session.commit()


class TestRemoveInvalidTokens:
    """Tests for remove_invalid_tokens function."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_remove_unregistered_tokens(self, async_session: AsyncSession):
        """Test removal of unregistered tokens from database."""
        await cleanup_push_tokens(async_session)
        try:
            # Create test user
            user = User(
                email=f"test-fcm-{uuid.uuid4()}@example.com",
                password_hash=hash_password("testpass123"),
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            # Create push tokens
            tokens = [
                PushToken(
                    user_id=user.id, token="test-valid-token", platform="android"
                ),
                PushToken(
                    user_id=user.id, token="test-unregistered-token", platform="android"
                ),
                PushToken(
                    user_id=user.id, token="test-invalid-token", platform="android"
                ),
            ]
            for token in tokens:
                async_session.add(token)
            await async_session.commit()

            # Simulate send results
            results: list[tuple[bool, str | None]] = [
                (True, None),
                (False, "UNREGISTERED"),
                (False, "INVALID_ARGUMENT"),
            ]

            removed = await remove_invalid_tokens(
                db=async_session,
                user_id=user.id,
                tokens=[
                    "test-valid-token",
                    "test-unregistered-token",
                    "test-invalid-token",
                ],
                results=results,
            )

            # Should remove unregistered and invalid tokens
            assert len(removed) == 2
            assert "test-unregistered-token" in removed
            assert "test-invalid-token" in removed

            # Verify in database
            stmt = select(PushToken).where(PushToken.user_id == user.id)
            result = await async_session.execute(stmt)
            remaining_tokens = result.scalars().all()

            assert len(remaining_tokens) == 1
            assert remaining_tokens[0].token == "test-valid-token"
        finally:
            await cleanup_push_tokens(async_session)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_remove_tokens_no_permanent_errors(self, async_session: AsyncSession):
        """Test that transient errors don't remove tokens."""
        await cleanup_push_tokens(async_session)
        try:
            # Create test user
            user = User(
                email=f"test-fcm-transient-{uuid.uuid4()}@example.com",
                password_hash=hash_password("testpass123"),
            )
            async_session.add(user)
            await async_session.commit()
            await async_session.refresh(user)

            # Create push token
            token = PushToken(
                user_id=user.id,
                token="test-temporarily-failed-token",
                platform="android",
            )
            async_session.add(token)
            await async_session.commit()

            # Simulate transient failure (not UNREGISTERED or INVALID_ARGUMENT)
            results: list[tuple[bool, str | None]] = [(False, "INTERNAL")]

            removed = await remove_invalid_tokens(
                db=async_session,
                user_id=user.id,
                tokens=["test-temporarily-failed-token"],
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


class TestGetFCMClient:
    """Tests for get_fcm_client function."""

    @patch("app.services.fcm.get_settings")
    def test_get_fcm_client_returns_singleton(self, mock_settings):
        """Test that get_fcm_client returns singleton instance."""
        mock_settings.return_value = MagicMock(
            FCM_PROJECT_ID=None,
            FCM_CREDENTIALS_PATH=None,
            GOOGLE_APPLICATION_CREDENTIALS=None,
        )

        client1 = get_fcm_client()
        client2 = get_fcm_client()

        assert client1 is client2
