"""Firebase Cloud Messaging service for Android push notifications.

This module provides FCM integration for sending push notifications
to Android devices via Firebase Admin SDK.
"""

from typing import Any
from uuid import UUID

import firebase_admin
import structlog
from firebase_admin import credentials, exceptions, messaging
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.notification import PushToken

logger = structlog.get_logger(__name__)


class FCMError(Exception):
    """Base exception for FCM errors."""

    def __init__(self, message: str, retriable: bool = False):
        self.message = message
        self.retriable = retriable
        super().__init__(message)


class FCMConfigError(FCMError):
    """Raised when FCM is misconfigured."""

    def __init__(self, detail: str):
        super().__init__(f"FCM configuration error: {detail}", retriable=False)


class FCMUnavailableError(FCMError):
    """Raised when FCM service is unavailable."""

    def __init__(self, detail: str = ""):
        message = "FCM service unavailable"
        if detail:
            message += f": {detail}"
        super().__init__(message, retriable=True)


class FCMClient:
    """Firebase Cloud Messaging client for sending push notifications.

    Handles initialization, sending notifications, and managing invalid tokens.
    Supports both single and batch (multicast) sending.
    """

    _instance: FCMClient | None = None
    _initialized: bool = False

    def __new__(cls) -> FCMClient:
        """Singleton pattern to ensure single Firebase app instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialize FCM client with Firebase credentials."""
        if FCMClient._initialized:
            return

        settings = get_settings()

        if not settings.FCM_PROJECT_ID and not settings.FCM_CREDENTIALS_PATH:
            logger.warning("FCM not configured - push notifications disabled")
            return

        try:
            cred = None
            options: dict[str, Any] = {}

            if settings.FCM_CREDENTIALS_PATH:
                cred = credentials.Certificate(settings.FCM_CREDENTIALS_PATH)
            elif settings.GOOGLE_APPLICATION_CREDENTIALS:
                cred = credentials.Certificate(settings.GOOGLE_APPLICATION_CREDENTIALS)

            if settings.FCM_PROJECT_ID:
                options["projectId"] = settings.FCM_PROJECT_ID

            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred, options)

            FCMClient._initialized = True
            logger.info("FCM client initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize FCM: {e}")
            raise FCMConfigError(str(e)) from e

    @property
    def is_configured(self) -> bool:
        """Check if FCM is properly configured."""
        return FCMClient._initialized

    def _build_message(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> messaging.Message:
        """Build FCM message with notification and optional data payload."""
        return messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=data or {},
            token=token,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                ),
            ),
        )

    def send_notification(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> tuple[bool, str | None]:
        """Send a push notification to a single device.

        Args:
            token: FCM device registration token
            title: Notification title
            body: Notification body text
            data: Optional data payload (key-value pairs, values must be strings)

        Returns:
            Tuple of (success, error_code or None)
            error_code is set for permanent failures (e.g., "UNREGISTERED")
        """
        if not self.is_configured:
            logger.warning("FCM not configured, skipping notification")
            return False, "NOT_CONFIGURED"

        settings = get_settings()

        try:
            message = self._build_message(token, title, body, data)
            response = messaging.send(message, dry_run=settings.FCM_DRY_RUN)
            logger.debug(f"FCM message sent: {response}")
            return True, None

        except messaging.UnregisteredError:
            logger.info(f"FCM token unregistered: {token[:20]}...")
            return False, "UNREGISTERED"

        except exceptions.InvalidArgumentError as e:
            logger.warning(f"FCM invalid argument: {e}")
            return False, "INVALID_ARGUMENT"

        except exceptions.UnavailableError as e:
            logger.error(f"FCM service unavailable: {e}")
            raise FCMUnavailableError(str(e)) from e

        except exceptions.DeadlineExceededError as e:
            logger.error(f"FCM request timeout: {e}")
            raise FCMUnavailableError("Request timeout") from e

        except exceptions.FirebaseError as e:
            logger.error(f"FCM error: {e.code} - {e.message}")
            if e.code in ("INTERNAL", "UNAVAILABLE"):
                raise FCMUnavailableError(e.message) from e
            return False, e.code

    def send_multicast(
        self,
        tokens: list[str],
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> list[tuple[bool, str | None]]:
        """Send the same notification to multiple devices.

        Args:
            tokens: List of FCM device registration tokens
            title: Notification title
            body: Notification body text
            data: Optional data payload

        Returns:
            List of (success, error_code) tuples, one per token in same order
        """
        if not self.is_configured:
            logger.warning("FCM not configured, skipping multicast")
            return [(False, "NOT_CONFIGURED")] * len(tokens)

        if not tokens:
            return []

        settings = get_settings()

        try:
            multicast = messaging.MulticastMessage(
                notification=messaging.Notification(title=title, body=body),
                data=data or {},
                tokens=tokens,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        click_action="FLUTTER_NOTIFICATION_CLICK",
                    ),
                ),
            )

            response = messaging.send_each_for_multicast(
                multicast, dry_run=settings.FCM_DRY_RUN
            )

            results: list[tuple[bool, str | None]] = []
            for resp in response.responses:
                if resp.success:
                    results.append((True, None))
                else:
                    error_code = self._extract_error_code(resp.exception)
                    results.append((False, error_code))

            logger.info(
                f"FCM multicast: {response.success_count} sent, "
                f"{response.failure_count} failed"
            )
            return results

        except exceptions.UnavailableError as e:
            logger.error(f"FCM service unavailable: {e}")
            raise FCMUnavailableError(str(e)) from e

        except exceptions.FirebaseError as e:
            logger.error(f"FCM multicast error: {e.code} - {e.message}")
            if e.code in ("INTERNAL", "UNAVAILABLE"):
                raise FCMUnavailableError(e.message) from e
            return [(False, e.code)] * len(tokens)

    def _extract_error_code(self, exception: Exception | None) -> str | None:
        """Extract error code from FCM exception."""
        if exception is None:
            return None

        if isinstance(exception, messaging.UnregisteredError):
            return "UNREGISTERED"
        if isinstance(exception, exceptions.InvalidArgumentError):
            return "INVALID_ARGUMENT"
        if isinstance(exception, exceptions.FirebaseError):
            return str(exception.code) if exception.code else None

        return "UNKNOWN"


async def remove_invalid_tokens(
    db: AsyncSession,
    user_id: UUID,
    tokens: list[str],
    results: list[tuple[bool, str | None]],
) -> list[str]:
    """Remove invalid tokens from the database based on send results.

    Args:
        db: Database session
        user_id: User ID owning the tokens
        tokens: List of tokens that were sent to
        results: Corresponding results from send_multicast

    Returns:
        List of tokens that were removed
    """
    tokens_to_remove: list[str] = []
    permanent_errors = {"UNREGISTERED", "INVALID_ARGUMENT"}

    for token, (success, error_code) in zip(tokens, results, strict=False):
        if not success and error_code in permanent_errors:
            tokens_to_remove.append(token)

    if tokens_to_remove:
        stmt = delete(PushToken).where(
            PushToken.user_id == user_id,
            PushToken.token.in_(tokens_to_remove),
        )
        result = await db.execute(stmt)
        await db.flush()
        logger.info(f"Removed {result.rowcount} invalid FCM tokens for user {user_id}")  # type: ignore[attr-defined]

    return tokens_to_remove


def get_fcm_client() -> FCMClient:
    """Get or create the FCM client singleton."""
    return FCMClient()
