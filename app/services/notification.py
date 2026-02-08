"""Unified notification service for push notifications.

This module provides a unified interface for sending push notifications
to users via FCM (Android) and APNs (iOS) with preferences checking,
throttling, quiet hours enforcement, and delivery logging.
"""

from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    NotificationLog,
    NotificationPreference,
    PushToken,
)
from app.services.apns import APNsClient, APNsUnavailableError, get_apns_client
from app.services.apns import remove_invalid_tokens as remove_invalid_apns_tokens
from app.services.fcm import FCMClient, FCMUnavailableError, get_fcm_client
from app.services.fcm import remove_invalid_tokens as remove_invalid_fcm_tokens
from app.services.notification_throttle import ThrottleManager

logger = structlog.get_logger(__name__)

# Mapping of notification types to preference fields
NOTIFICATION_TYPE_TO_PREFERENCE = {
    "feeding_reminder": "feeding_reminders",
    "overdue_alert": "overdue_alerts",
    "streak_protection": "streak_protection",
    "weekly_summary": "weekly_summary",
    "family_update": "family_updates",
    "marketing": "marketing",
}

# Default preferences when user has no preferences record
DEFAULT_PREFERENCES = {
    "global_opt_out": False,
    "timezone": None,
    "feeding_reminders": True,
    "overdue_alerts": True,
    "streak_protection": True,
    "weekly_summary": True,
    "family_updates": True,
    "marketing": False,
}


class NotificationService:
    """Unified service for sending push notifications.

    Handles routing between FCM (Android) and APNs (iOS),
    preferences checking, throttling, quiet hours, and delivery logging.
    """

    def __init__(self, db: AsyncSession, redis: Redis | None = None):
        """Initialize notification service.

        Args:
            db: Database session for queries and logging
            redis: Redis client for throttling (optional, disables throttling if None)
        """
        self.db = db
        self._redis = redis
        self._fcm_client: FCMClient | None = None
        self._apns_client: APNsClient | None = None
        self._throttle_manager: ThrottleManager | None = None

    @property
    def throttle_manager(self) -> ThrottleManager | None:
        """Get throttle manager (lazy initialization)."""
        if self._throttle_manager is None and self._redis is not None:
            self._throttle_manager = ThrottleManager(self._redis, self.db)
        return self._throttle_manager

    @property
    def fcm_client(self) -> FCMClient:
        """Get FCM client (lazy initialization)."""
        if self._fcm_client is None:
            self._fcm_client = get_fcm_client()
        return self._fcm_client

    @property
    def apns_client(self) -> APNsClient:
        """Get APNs client (lazy initialization)."""
        if self._apns_client is None:
            self._apns_client = get_apns_client()
        return self._apns_client

    async def register_push_token(
        self,
        user_id: UUID,
        token: str,
        platform: str,
    ) -> None:
        """Register a push notification token for a user.

        If the token already exists, updates the platform and timestamp.

        Args:
            user_id: User ID to register token for
            token: Device push token
            platform: Platform type ("ios" or "android")
        """
        stmt = (
            insert(PushToken)
            .values(
                user_id=user_id,
                token=token,
                platform=platform,
            )
            .on_conflict_do_update(
                constraint="uq_user_push_token",
                set_={
                    "platform": platform,
                    "updated_at": func.now(),
                },
            )
        )
        await self.db.execute(stmt)
        await self.db.flush()
        logger.info(f"Registered {platform} push token for user {user_id}")

    async def unregister_push_token(
        self,
        user_id: UUID,
        token: str,
    ) -> bool:
        """Remove a push notification token.

        Args:
            user_id: User ID owning the token
            token: Token to remove

        Returns:
            True if token was removed, False if not found
        """
        stmt = delete(PushToken).where(
            PushToken.user_id == user_id,
            PushToken.token == token,
        )
        result = await self.db.execute(stmt)
        await self.db.flush()

        if result.rowcount > 0:  # type: ignore[attr-defined]
            logger.info(f"Unregistered push token for user {user_id}")
            return True
        return False

    async def get_user_preferences(
        self,
        user_id: UUID,
    ) -> dict[str, bool | str | None]:
        """Get notification preferences for a user.

        Args:
            user_id: User ID to get preferences for

        Returns:
            Dict of preference name to value
        """
        stmt = select(NotificationPreference).where(
            NotificationPreference.user_id == user_id
        )
        result = await self.db.execute(stmt)
        preferences = result.scalar_one_or_none()

        if preferences is None:
            return DEFAULT_PREFERENCES.copy()  # type: ignore[return-value]

        return {
            "global_opt_out": preferences.global_opt_out,
            "timezone": preferences.timezone,
            "feeding_reminders": preferences.feeding_reminders,
            "overdue_alerts": preferences.overdue_alerts,
            "streak_protection": preferences.streak_protection,
            "weekly_summary": preferences.weekly_summary,
            "family_updates": preferences.family_updates,
            "marketing": preferences.marketing,
        }

    async def update_preferences(
        self,
        user_id: UUID,
        prefs: dict[str, bool | str | None],
    ) -> dict[str, bool | str | None]:
        """Update notification preferences for a user.

        Args:
            user_id: User ID to update preferences for
            prefs: Dict of preference name to value

        Returns:
            Updated preferences dict
        """
        stmt = select(NotificationPreference).where(
            NotificationPreference.user_id == user_id
        )
        result = await self.db.execute(stmt)
        preferences = result.scalar_one_or_none()

        if preferences is None:
            # Create new preferences with defaults + provided values
            preferences = NotificationPreference(
                user_id=user_id,
                global_opt_out=prefs.get("global_opt_out", False),
                timezone=prefs.get("timezone"),
                feeding_reminders=prefs.get("feeding_reminders", True),
                overdue_alerts=prefs.get("overdue_alerts", True),
                streak_protection=prefs.get("streak_protection", True),
                weekly_summary=prefs.get("weekly_summary", True),
                family_updates=prefs.get("family_updates", True),
                marketing=prefs.get("marketing", False),
            )
            self.db.add(preferences)
        else:
            # Update existing preferences
            for field, value in prefs.items():
                if hasattr(preferences, field):
                    setattr(preferences, field, value)

        await self.db.flush()
        await self.db.refresh(preferences)

        return await self.get_user_preferences(user_id)

    async def _get_user_tokens(
        self,
        user_id: UUID,
    ) -> list[PushToken]:
        """Get all push tokens for a user.

        Args:
            user_id: User ID to get tokens for

        Returns:
            List of PushToken objects
        """
        stmt = select(PushToken).where(PushToken.user_id == user_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    def _is_notification_allowed(
        self,
        preferences: dict[str, bool],
        notification_type: str | None,
    ) -> bool:
        """Check if notification type is allowed by user preferences.

        Args:
            preferences: User's notification preferences
            notification_type: Type of notification to check

        Returns:
            True if notification is allowed, False otherwise
        """
        if notification_type is None:
            return True

        pref_field = NOTIFICATION_TYPE_TO_PREFERENCE.get(notification_type)
        if pref_field is None:
            # Unknown notification type, allow by default
            logger.warning(f"Unknown notification type: {notification_type}")
            return True

        return preferences.get(pref_field, True)

    async def _log_notification(
        self,
        user_id: UUID,
        notification_type: str,
        title: str,
        body: str,
        platform: str | None,
        success: bool,
        error_code: str | None = None,
    ) -> None:
        """Log a notification delivery attempt.

        Args:
            user_id: User who received the notification
            notification_type: Type of notification
            title: Notification title
            body: Notification body
            platform: Platform ("ios", "android", or None)
            success: Whether delivery succeeded
            error_code: Error code if failed
        """
        log_entry = NotificationLog(
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            body=body,
            platform=platform,
            success=success,
            error_code=error_code,
        )
        self.db.add(log_entry)
        await self.db.flush()

    async def send_push(
        self,
        user_id: UUID,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        notification_type: str | None = None,
        bypass_throttle: bool = False,
    ) -> bool:
        """Send a push notification to a user.

        Routes to appropriate service (FCM/APNs) based on token platform.
        Checks user preferences, throttle limits, and quiet hours before sending.

        Args:
            user_id: User ID to send notification to
            title: Notification title
            body: Notification body text
            data: Optional custom data payload
            notification_type: Type of notification for preference checking
            bypass_throttle: Skip throttle checks (use for system notifications)

        Returns:
            True if at least one notification was sent successfully
        """
        # Check throttle limits (global opt-out, daily limit, quiet hours)
        if not bypass_throttle and self.throttle_manager is not None:
            throttle_result = await self.throttle_manager.can_send_notification(
                user_id, notification_type
            )
            if not throttle_result:
                logger.info(
                    f"Notification throttled for user {user_id}: {throttle_result.reason}"
                )
                await self._log_notification(
                    user_id=user_id,
                    notification_type=notification_type or "general",
                    title=title,
                    body=body,
                    platform=None,
                    success=False,
                    error_code=throttle_result.reason,
                )
                return False

        # Check per-type preferences
        preferences = await self.get_user_preferences(user_id)
        if not self._is_notification_allowed(preferences, notification_type):  # type: ignore[arg-type]
            logger.info(
                f"Notification type '{notification_type}' disabled for user {user_id}"
            )
            await self._log_notification(
                user_id=user_id,
                notification_type=notification_type or "general",
                title=title,
                body=body,
                platform=None,
                success=False,
                error_code="PREFERENCE_DISABLED",
            )
            return False

        # Get user tokens
        tokens = await self._get_user_tokens(user_id)
        if not tokens:
            logger.info(f"No push tokens found for user {user_id}")
            return False

        # Group tokens by platform
        ios_tokens = [t for t in tokens if t.platform == "ios"]
        android_tokens = [t for t in tokens if t.platform == "android"]

        any_success = False

        # Send to iOS devices
        if ios_tokens:
            ios_success = await self._send_to_ios(
                user_id, ios_tokens, title, body, data, notification_type
            )
            any_success = any_success or ios_success

        # Send to Android devices
        if android_tokens:
            android_success = await self._send_to_android(
                user_id, android_tokens, title, body, data, notification_type
            )
            any_success = any_success or android_success

        # Increment throttle counter on successful send
        if any_success and not bypass_throttle and self.throttle_manager is not None:
            await self.throttle_manager.increment_counter(user_id)

        return any_success

    async def _send_to_ios(
        self,
        user_id: UUID,
        tokens: list[PushToken],
        title: str,
        body: str,
        data: dict[str, Any] | None,
        notification_type: str | None,
    ) -> bool:
        """Send notification to iOS devices.

        Args:
            user_id: User ID
            tokens: List of iOS push tokens
            title: Notification title
            body: Notification body
            data: Custom data payload
            notification_type: Type of notification

        Returns:
            True if at least one notification was sent successfully
        """
        if not self.apns_client.is_configured:
            logger.warning("APNs not configured, skipping iOS notifications")
            return False

        token_strings = [t.token for t in tokens]
        any_success = False

        try:
            results = await self.apns_client.send_batch(
                token_strings, title, body, data
            )

            # Process results and log
            for _token_obj, (success, error) in zip(tokens, results, strict=False):
                await self._log_notification(
                    user_id=user_id,
                    notification_type=notification_type or "general",
                    title=title,
                    body=body,
                    platform="ios",
                    success=success,
                    error_code=error,
                )
                if success:
                    any_success = True

            # Remove invalid tokens
            await remove_invalid_apns_tokens(
                self.db, user_id, token_strings, results
            )

        except APNsUnavailableError as e:
            logger.error(f"APNs unavailable: {e}")
            # Log failure for all tokens
            for _ in tokens:
                await self._log_notification(
                    user_id=user_id,
                    notification_type=notification_type or "general",
                    title=title,
                    body=body,
                    platform="ios",
                    success=False,
                    error_code="SERVICE_UNAVAILABLE",
                )

        return any_success

    async def _send_to_android(
        self,
        user_id: UUID,
        tokens: list[PushToken],
        title: str,
        body: str,
        data: dict[str, Any] | None,
        notification_type: str | None,
    ) -> bool:
        """Send notification to Android devices.

        Args:
            user_id: User ID
            tokens: List of Android push tokens
            title: Notification title
            body: Notification body
            data: Custom data payload
            notification_type: Type of notification

        Returns:
            True if at least one notification was sent successfully
        """
        if not self.fcm_client.is_configured:
            logger.warning("FCM not configured, skipping Android notifications")
            return False

        token_strings = [t.token for t in tokens]
        any_success = False

        # Convert data values to strings for FCM
        fcm_data: dict[str, str] | None = None
        if data:
            fcm_data = {k: str(v) for k, v in data.items()}

        try:
            results = self.fcm_client.send_multicast(
                token_strings, title, body, fcm_data
            )

            # Process results and log
            for _token_obj, (success, error) in zip(tokens, results, strict=False):
                await self._log_notification(
                    user_id=user_id,
                    notification_type=notification_type or "general",
                    title=title,
                    body=body,
                    platform="android",
                    success=success,
                    error_code=error,
                )
                if success:
                    any_success = True

            # Remove invalid tokens
            await remove_invalid_fcm_tokens(
                self.db, user_id, token_strings, results
            )

        except FCMUnavailableError as e:
            logger.error(f"FCM unavailable: {e}")
            # Log failure for all tokens
            for _ in tokens:
                await self._log_notification(
                    user_id=user_id,
                    notification_type=notification_type or "general",
                    title=title,
                    body=body,
                    platform="android",
                    success=False,
                    error_code="SERVICE_UNAVAILABLE",
                )

        return any_success

    async def send_push_batch(
        self,
        user_ids: list[UUID],
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        notification_type: str | None = None,
    ) -> list[bool]:
        """Send the same notification to multiple users.

        Args:
            user_ids: List of user IDs to send to
            title: Notification title
            body: Notification body text
            data: Optional custom data payload
            notification_type: Type of notification for preference checking

        Returns:
            List of success booleans, one per user in same order
        """
        results: list[bool] = []

        for user_id in user_ids:
            success = await self.send_push(
                user_id=user_id,
                title=title,
                body=body,
                data=data,
                notification_type=notification_type,
            )
            results.append(success)

        return results


async def get_notification_service(
    db: AsyncSession,
    redis: Redis | None = None,
) -> NotificationService:
    """Create a notification service instance.

    Args:
        db: Database session
        redis: Optional Redis client for throttling support

    Returns:
        NotificationService instance
    """
    return NotificationService(db, redis)
