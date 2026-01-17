"""Apple Push Notification service for iOS push notifications.

This module provides APNs integration for sending push notifications
to iOS devices via aioapns library with token-based authentication.
"""

import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from aioapns import APNs, NotificationRequest, PushType
from aioapns.common import APNS_RESPONSE_CODE
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.notification import PushToken

logger = logging.getLogger(__name__)


class APNsError(Exception):
    """Base exception for APNs errors."""

    def __init__(self, message: str, retriable: bool = False):
        self.message = message
        self.retriable = retriable
        super().__init__(message)


class APNsConfigError(APNsError):
    """Raised when APNs is misconfigured."""

    def __init__(self, detail: str):
        super().__init__(f"APNs configuration error: {detail}", retriable=False)


class APNsUnavailableError(APNsError):
    """Raised when APNs service is unavailable."""

    def __init__(self, detail: str = ""):
        message = "APNs service unavailable"
        if detail:
            message += f": {detail}"
        super().__init__(message, retriable=True)


class APNsClient:
    """Apple Push Notification service client for sending push notifications.

    Handles initialization with .p8 key, sending notifications, and managing
    invalid tokens. Supports both single and batch sending.
    """

    _instance: APNsClient | None = None
    _initialized: bool = False
    _client: APNs | None = None

    def __new__(cls) -> APNsClient:
        """Singleton pattern to ensure single APNs client instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialize APNs client with .p8 key credentials."""
        if APNsClient._initialized:
            return

        settings = get_settings()

        if not all([
            settings.APNS_KEY_ID,
            settings.APNS_TEAM_ID,
            settings.APNS_BUNDLE_ID,
            settings.APNS_KEY_PATH,
        ]):
            logger.warning("APNs not configured - iOS push notifications disabled")
            return

        try:
            assert settings.APNS_KEY_PATH is not None  # Checked above
            key_path = Path(settings.APNS_KEY_PATH)
            if not key_path.exists():
                raise APNsConfigError(f"Key file not found: {settings.APNS_KEY_PATH}")

            key_content = key_path.read_text()

            APNsClient._client = APNs(
                key=key_content,
                key_id=settings.APNS_KEY_ID,
                team_id=settings.APNS_TEAM_ID,
                topic=settings.APNS_BUNDLE_ID,
                use_sandbox=settings.APNS_USE_SANDBOX,
            )

            APNsClient._initialized = True
            mode = "sandbox" if settings.APNS_USE_SANDBOX else "production"
            logger.info(f"APNs client initialized successfully ({mode} mode)")

        except APNsConfigError:
            raise
        except Exception as e:
            logger.error(f"Failed to initialize APNs: {e}")
            raise APNsConfigError(str(e)) from e

    @property
    def is_configured(self) -> bool:
        """Check if APNs is properly configured."""
        return APNsClient._initialized and APNsClient._client is not None

    def _build_payload(
        self,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        badge: int | None = None,
    ) -> dict[str, Any]:
        """Build APNs payload following Apple specification."""
        aps: dict[str, Any] = {
            "alert": {
                "title": title,
                "body": body,
            },
            "sound": "default",
        }

        if badge is not None:
            aps["badge"] = badge

        payload: dict[str, Any] = {"aps": aps}

        if data:
            payload.update(data)

        return payload

    async def send_notification(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        badge: int | None = None,
    ) -> tuple[bool, str | None]:
        """Send a push notification to a single iOS device.

        Args:
            token: APNs device token
            title: Notification title
            body: Notification body text
            data: Optional custom data payload
            badge: Optional badge number to display on app icon

        Returns:
            Tuple of (success, error_reason or None)
            error_reason is set for failures (e.g., "BadDeviceToken")
        """
        if not self.is_configured:
            logger.warning("APNs not configured, skipping notification")
            return False, "NOT_CONFIGURED"

        try:
            payload = self._build_payload(title, body, data, badge)
            request = NotificationRequest(
                device_token=token,
                message=payload,
                notification_id=str(uuid4()),
                push_type=PushType.ALERT,
            )

            assert APNsClient._client is not None  # Checked by is_configured
            result = await APNsClient._client.send_notification(request)

            if result.is_successful:
                logger.debug(f"APNs notification sent: {result.notification_id}")
                return True, None

            error_reason = result.description or f"HTTP_{result.status}"
            logger.info(
                f"APNs notification failed: {result.status} - {result.description}"
            )

            if result.status == APNS_RESPONSE_CODE.SERVICE_UNAVAILABLE:
                raise APNsUnavailableError(result.description or "Service unavailable")

            if result.status == APNS_RESPONSE_CODE.INTERNAL_SERVER_ERROR:
                raise APNsUnavailableError(result.description or "Internal server error")

            return False, error_reason

        except APNsUnavailableError:
            raise
        except Exception as e:
            logger.error(f"APNs error: {e}")
            raise APNsUnavailableError(str(e)) from e

    async def send_batch(
        self,
        tokens: list[str],
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        badge: int | None = None,
    ) -> list[tuple[bool, str | None]]:
        """Send the same notification to multiple iOS devices.

        Args:
            tokens: List of APNs device tokens
            title: Notification title
            body: Notification body text
            data: Optional custom data payload
            badge: Optional badge number

        Returns:
            List of (success, error_reason) tuples, one per token in same order
        """
        if not self.is_configured:
            logger.warning("APNs not configured, skipping batch")
            return [(False, "NOT_CONFIGURED")] * len(tokens)

        if not tokens:
            return []

        results: list[tuple[bool, str | None]] = []
        success_count = 0
        failure_count = 0

        for token in tokens:
            try:
                success, error = await self.send_notification(
                    token, title, body, data, badge
                )
                results.append((success, error))
                if success:
                    success_count += 1
                else:
                    failure_count += 1
            except APNsUnavailableError:
                raise
            except Exception as e:
                logger.error(f"APNs batch error for token: {e}")
                results.append((False, "SEND_ERROR"))
                failure_count += 1

        logger.info(f"APNs batch: {success_count} sent, {failure_count} failed")
        return results


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
        results: Corresponding results from send_batch

    Returns:
        List of tokens that were removed
    """
    tokens_to_remove: list[str] = []
    permanent_errors = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}

    for token, (success, error_reason) in zip(tokens, results, strict=False):
        if not success and error_reason in permanent_errors:
            tokens_to_remove.append(token)

    if tokens_to_remove:
        stmt = delete(PushToken).where(
            PushToken.user_id == user_id,
            PushToken.token.in_(tokens_to_remove),
        )
        result = await db.execute(stmt)
        await db.commit()
        logger.info(f"Removed {result.rowcount} invalid APNs tokens for user {user_id}")  # type: ignore[attr-defined]

    return tokens_to_remove


def get_apns_client() -> APNsClient:
    """Get or create the APNs client singleton."""
    return APNsClient()
