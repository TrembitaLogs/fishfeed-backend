"""Notification throttling and quiet hours management.

This module provides rate limiting and quiet hours functionality for
push notifications to prevent spam and respect user preferences.
"""

from datetime import UTC, date, datetime, time, timezone
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.notification import NotificationPreference

logger = structlog.get_logger(__name__)
settings = get_settings()

# Throttling constants
MAX_NOTIFICATIONS_PER_DAY = 10
QUIET_HOURS_START = time(22, 0)  # 22:00
QUIET_HOURS_END = time(8, 0)  # 08:00

# Redis key prefix for notification counters
NOTIFICATION_COUNT_PREFIX = f"{settings.REDIS_KEY_PREFIX}notification_count"

# Notification types that bypass quiet hours (critical alerts)
URGENT_NOTIFICATION_TYPES = frozenset({
    "critical_alert",
    "security_alert",
    "account_alert",
})


class ThrottleResult:
    """Result of throttle check with reason."""

    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:
        return f"ThrottleResult(allowed={self.allowed}, reason='{self.reason}')"


class ThrottleManager:
    """Manages notification throttling and quiet hours.

    Provides rate limiting (max notifications per day) and quiet hours
    enforcement (22:00-08:00) for push notifications.
    """

    def __init__(self, redis: Redis, db: AsyncSession):
        """Initialize throttle manager.

        Args:
            redis: Redis client for counters.
            db: Database session for preferences lookup.
        """
        self.redis = redis
        self.db = db

    def _get_counter_key(self, user_id: UUID) -> str:
        """Get Redis key for user's daily notification counter.

        Args:
            user_id: User ID.

        Returns:
            Redis key string.
        """
        today = date.today().isoformat()
        return f"{NOTIFICATION_COUNT_PREFIX}:{user_id}:{today}"

    async def get_daily_count(self, user_id: UUID) -> int:
        """Get current daily notification count for a user.

        Args:
            user_id: User ID.

        Returns:
            Current count of notifications sent today.
        """
        key = self._get_counter_key(user_id)
        count = await self.redis.get(key)
        return int(count) if count else 0

    async def check_daily_limit(self, user_id: UUID) -> bool:
        """Check if user has reached daily notification limit.

        Args:
            user_id: User ID.

        Returns:
            True if under limit (can send), False if limit reached.
        """
        count = await self.get_daily_count(user_id)
        return count < MAX_NOTIFICATIONS_PER_DAY

    async def increment_counter(self, user_id: UUID) -> int:
        """Increment daily notification counter for a user.

        Counter expires at midnight (24 hours TTL for simplicity).

        Args:
            user_id: User ID.

        Returns:
            New counter value after increment.
        """
        key = self._get_counter_key(user_id)

        # Use pipeline for atomic increment and expire
        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, 86400)  # 24 hours TTL
        results = await pipe.execute()

        new_count = int(results[0])
        logger.debug(f"Incremented notification counter for user {user_id}: {new_count}")
        return new_count

    async def get_user_timezone(self, user_id: UUID) -> timezone:
        """Get user's timezone from preferences.

        Args:
            user_id: User ID.

        Returns:
            User's timezone or UTC as default.
        """
        stmt = select(NotificationPreference.timezone).where(
            NotificationPreference.user_id == user_id
        )
        result = await self.db.execute(stmt)
        tz_str = result.scalar_one_or_none()

        if tz_str:
            try:
                # Parse timezone offset string like "+02:00" or "-05:00"
                if tz_str.startswith(("+", "-")):
                    sign = 1 if tz_str[0] == "+" else -1
                    hours, minutes = map(int, tz_str[1:].split(":"))
                    from datetime import timedelta

                    offset = timedelta(hours=hours, minutes=minutes) * sign
                    return timezone(offset)
            except (ValueError, AttributeError):
                logger.warning(f"Invalid timezone '{tz_str}' for user {user_id}, using UTC")

        return UTC

    def is_quiet_hours(
        self,
        user_timezone: timezone,
        check_time: datetime | None = None,
    ) -> bool:
        """Check if current time is within quiet hours.

        Quiet hours are 22:00 to 08:00 in user's timezone.

        Args:
            user_timezone: User's timezone.
            check_time: Optional time to check (defaults to now).

        Returns:
            True if within quiet hours, False otherwise.
        """
        if check_time is None:
            check_time = datetime.now(UTC)

        # Convert to user's local time
        local_time = check_time.astimezone(user_timezone).time()

        # Quiet hours span midnight: 22:00 -> 08:00
        # So we check if time is >= 22:00 OR < 08:00
        return local_time >= QUIET_HOURS_START or local_time < QUIET_HOURS_END

    async def check_quiet_hours(
        self,
        user_id: UUID,
        check_time: datetime | None = None,
    ) -> bool:
        """Check if current time is within quiet hours for a user.

        Args:
            user_id: User ID.
            check_time: Optional time to check (defaults to now).

        Returns:
            True if within quiet hours, False otherwise.
        """
        user_tz = await self.get_user_timezone(user_id)
        return self.is_quiet_hours(user_tz, check_time)

    async def is_globally_opted_out(self, user_id: UUID) -> bool:
        """Check if user has globally opted out of all notifications.

        Args:
            user_id: User ID.

        Returns:
            True if user has opted out of all notifications.
        """
        stmt = select(NotificationPreference.global_opt_out).where(
            NotificationPreference.user_id == user_id
        )
        result = await self.db.execute(stmt)
        global_opt_out = result.scalar_one_or_none()

        # If no preferences exist, not opted out (use defaults)
        return global_opt_out is True

    async def can_send_notification(
        self,
        user_id: UUID,
        notification_type: str | None = None,
    ) -> ThrottleResult:
        """Check if a notification can be sent to a user.

        Performs all throttling checks:
        1. Global opt-out
        2. Daily limit
        3. Quiet hours (unless urgent notification)

        Args:
            user_id: User ID.
            notification_type: Type of notification (for urgent bypass).

        Returns:
            ThrottleResult with allowed status and reason if blocked.
        """
        # Check global opt-out first
        if await self.is_globally_opted_out(user_id):
            logger.info(f"Notification blocked for user {user_id}: global opt-out")
            return ThrottleResult(False, "GLOBAL_OPT_OUT")

        # Check daily limit
        if not await self.check_daily_limit(user_id):
            logger.info(f"Notification blocked for user {user_id}: daily limit reached")
            return ThrottleResult(False, "DAILY_LIMIT_REACHED")

        # Check quiet hours (urgent notifications bypass this)
        is_urgent = notification_type in URGENT_NOTIFICATION_TYPES
        if not is_urgent and await self.check_quiet_hours(user_id):
            logger.info(f"Notification blocked for user {user_id}: quiet hours")
            return ThrottleResult(False, "QUIET_HOURS")

        return ThrottleResult(True)

    def get_next_send_time(self, user_timezone: timezone) -> datetime:
        """Get the next available time to send notification after quiet hours.

        Args:
            user_timezone: User's timezone.

        Returns:
            Next datetime when notifications can be sent (08:00 user's time).
        """
        now = datetime.now(UTC)
        local_now = now.astimezone(user_timezone)

        # Create 08:00 today in user's timezone
        next_send = datetime.combine(
            local_now.date(),
            QUIET_HOURS_END,
            tzinfo=user_timezone,
        )

        # If we're past 08:00 today, schedule for tomorrow 08:00
        if local_now.time() >= QUIET_HOURS_END:
            from datetime import timedelta

            next_send += timedelta(days=1)

        return next_send.astimezone(UTC)


async def get_throttle_manager(redis: Redis, db: AsyncSession) -> ThrottleManager:
    """Create a throttle manager instance.

    Args:
        redis: Redis client.
        db: Database session.

    Returns:
        ThrottleManager instance.
    """
    return ThrottleManager(redis, db)
