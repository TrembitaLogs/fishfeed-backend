"""Analytics service for tracking user events and GDPR compliance."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.ai import AIScan
from app.models.analytics import AnalyticsEvent
from app.models.aquarium import Aquarium, AquariumMember, FamilyInvite
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak
from app.models.notification import NotificationLog, NotificationPreference, PushToken
from app.models.user import RefreshToken, User
from app.schemas.analytics import DataExportResponse, EventRequest
from app.services.storage import S3StorageService, StorageNotConfiguredError

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 100


class AnalyticsError(Exception):
    """Base exception for analytics errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class BatchSizeExceededError(AnalyticsError):
    """Raised when batch size exceeds the limit."""

    def __init__(self, size: int):
        super().__init__(
            f"Batch size {size} exceeds maximum of {MAX_BATCH_SIZE}",
            status_code=400,
        )


def hash_ip(ip: str) -> str:
    """Hash IP address using SHA-256 with salt.

    Args:
        ip: Raw IP address string.

    Returns:
        64-character hex string of hashed IP.
    """
    settings = get_settings()
    salted = f"{settings.ANALYTICS_IP_SALT}:{ip}"
    return hashlib.sha256(salted.encode()).hexdigest()


async def track_event(
    db: AsyncSession,
    user_id: UUID,
    event: EventRequest,
    ip: str,
) -> None:
    """Track a single analytics event.

    Saves the event to PostgreSQL and optionally forwards to external service.

    Args:
        db: Database session.
        user_id: User who triggered the event.
        event: Event data.
        ip: Client IP address (will be hashed).
    """
    ip_hash = hash_ip(ip)
    timestamp = event.timestamp or datetime.now(UTC)

    analytics_event = AnalyticsEvent(
        user_id=user_id,
        event_type=event.event_type,
        properties=event.properties,
        device_info=event.device_info,
        ip_hash=ip_hash,
        created_at=timestamp,
    )

    db.add(analytics_event)
    await db.commit()

    logger.debug(
        f"Tracked event '{event.event_type}' for user '{user_id}'"
    )

    # Fire-and-forget external forwarding
    settings = get_settings()
    if settings.ANALYTICS_FORWARD_URL:
        event_data = {
            "user_id": str(user_id),
            "event_type": event.event_type,
            "properties": event.properties,
            "device_info": event.device_info,
            "timestamp": timestamp.isoformat(),
        }
        asyncio.create_task(_forward_to_external_safe([event_data]))


async def track_events_batch(
    db: AsyncSession,
    user_id: UUID,
    events: list[EventRequest],
    ip: str,
) -> int:
    """Track multiple analytics events in a batch.

    Performs bulk insert for efficiency. Maximum 100 events per batch.

    Args:
        db: Database session.
        user_id: User who triggered the events.
        events: List of event data.
        ip: Client IP address (will be hashed).

    Returns:
        Number of events saved.

    Raises:
        BatchSizeExceededError: If batch size exceeds 100.
    """
    if len(events) > MAX_BATCH_SIZE:
        raise BatchSizeExceededError(len(events))

    if not events:
        return 0

    ip_hash = hash_ip(ip)
    now = datetime.now(UTC)

    # Prepare bulk insert data
    event_records = []
    forward_data = []

    for event in events:
        timestamp = event.timestamp or now
        event_records.append({
            "user_id": user_id,
            "event_type": event.event_type,
            "properties": event.properties,
            "device_info": event.device_info,
            "ip_hash": ip_hash,
            "created_at": timestamp,
        })
        forward_data.append({
            "user_id": str(user_id),
            "event_type": event.event_type,
            "properties": event.properties,
            "device_info": event.device_info,
            "timestamp": timestamp.isoformat(),
        })

    # Bulk insert
    stmt = insert(AnalyticsEvent).values(event_records)
    await db.execute(stmt)
    await db.commit()

    logger.info(
        f"Tracked {len(events)} events in batch for user '{user_id}'"
    )

    # Fire-and-forget external forwarding
    settings = get_settings()
    if settings.ANALYTICS_FORWARD_URL:
        asyncio.create_task(_forward_to_external_safe(forward_data))

    return len(events)


async def _forward_to_external_safe(events: list[dict]) -> None:
    """Wrapper for forward_to_external that catches all exceptions.

    This ensures fire-and-forget behavior without affecting the main flow.
    """
    try:
        await forward_to_external(events)
    except Exception as e:
        logger.error(f"Failed to forward events to external service: {e}")


async def forward_to_external(events: list[dict]) -> None:
    """Forward events to external analytics service (PostHog/Amplitude).

    Implements retry logic with exponential backoff.

    Args:
        events: List of event dictionaries to forward.

    Raises:
        httpx.HTTPError: If all retries fail.
    """
    settings = get_settings()
    url = settings.ANALYTICS_FORWARD_URL

    if not url:
        return

    max_retries = settings.ANALYTICS_FORWARD_MAX_RETRIES
    timeout = settings.ANALYTICS_FORWARD_TIMEOUT_SECONDS

    payload = {"events": events}

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()

            logger.debug(
                f"Successfully forwarded {len(events)} events to {url}"
            )
            return

        except httpx.HTTPError as e:
            if attempt < max_retries - 1:
                # Exponential backoff: 1s, 2s, 4s
                wait_time = 2 ** attempt
                logger.warning(
                    f"Failed to forward events (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {wait_time}s..."
                )
                await asyncio.sleep(wait_time)
            else:
                logger.error(
                    f"Failed to forward events after {max_retries} attempts: {e}"
                )
                raise


class GDPRError(AnalyticsError):
    """Raised when GDPR operation fails."""

    pass


class UserNotFoundError(GDPRError):
    """Raised when user is not found for GDPR operation."""

    def __init__(self, user_id: UUID):
        super().__init__(f"User {user_id} not found", status_code=404)


EXPORT_URL_TTL_SECONDS = 86400  # 24 hours


async def export_user_data(
    db: AsyncSession,
    user_id: UUID,
    storage: S3StorageService | None = None,
) -> DataExportResponse:
    """Export all user data as JSON and upload to S3.

    Collects data from all tables containing user information and
    generates a presigned S3 URL for download.

    Args:
        db: Database session.
        user_id: User ID to export data for.
        storage: S3 storage service (optional, for testing).

    Returns:
        DataExportResponse with presigned download URL.

    Raises:
        UserNotFoundError: If user does not exist.
        StorageNotConfiguredError: If S3 is not configured.
        GDPRError: If export fails.
    """
    # Verify user exists
    user_result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        raise UserNotFoundError(user_id)

    # Collect all user data
    export_data = await _collect_user_data(db, user_id, user)

    # Convert to JSON
    json_data = json.dumps(export_data, default=str, indent=2).encode("utf-8")

    # Upload to S3
    if storage is None:
        storage = S3StorageService()

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    object_key = f"gdpr-exports/{user_id}/{timestamp}_data_export.json"

    try:
        await storage.upload_json(json_data, object_key)
        download_url = await storage.generate_presigned_url(
            object_key, expires_in_seconds=EXPORT_URL_TTL_SECONDS
        )
        file_size = len(json_data)
    except StorageNotConfiguredError:
        raise
    except Exception as e:
        logger.error(f"GDPR export failed for user {user_id}: {e}")
        raise GDPRError(f"Failed to export user data: {e}") from None

    expires_at = datetime.now(UTC) + timedelta(seconds=EXPORT_URL_TTL_SECONDS)

    logger.info(
        f"GDPR data export completed for user {user_id}, "
        f"file size: {file_size} bytes"
    )

    return DataExportResponse(
        download_url=download_url,  # type: ignore[arg-type]
        expires_at=expires_at,
        file_size_bytes=file_size,
        format="json",
    )


async def _collect_user_data(
    db: AsyncSession,
    user_id: UUID,
    user: User,
) -> dict[str, Any]:
    """Collect all user data from database tables.

    Args:
        db: Database session.
        user_id: User ID.
        user: User model instance.

    Returns:
        Dictionary with all user data.
    """
    export_data: dict[str, Any] = {
        "exported_at": datetime.now(UTC).isoformat(),
        "user_id": str(user_id),
        "profile": {
            "id": str(user.id),
            "email": user.email,
            "nickname": user.nickname,
            "avatar_url": user.avatar_url,
            "email_verified": user.email_verified,
            "subscription_status": user.subscription_status,
            "subscription_expires_at": (
                user.subscription_expires_at.isoformat()
                if user.subscription_expires_at
                else None
            ),
            "settings": user.settings,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        },
    }

    # Aquariums (owned)
    aquariums_result = await db.execute(
        select(Aquarium).where(
            Aquarium.owner_id == user_id,
            Aquarium.deleted_at.is_(None),
        )
    )
    aquariums = aquariums_result.scalars().all()
    export_data["owned_aquariums"] = [
        {
            "id": str(a.id),
            "name": a.name,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in aquariums
    ]

    # Aquarium memberships
    memberships_result = await db.execute(
        select(AquariumMember).where(AquariumMember.user_id == user_id)
    )
    memberships = memberships_result.scalars().all()
    export_data["aquarium_memberships"] = [
        {
            "aquarium_id": str(m.aquarium_id),
            "role": m.role,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        }
        for m in memberships
    ]

    # Fish (from owned aquariums)
    aquarium_ids = [a.id for a in aquariums]
    if aquarium_ids:
        fish_result = await db.execute(
            select(Fish).where(
                Fish.aquarium_id.in_(aquarium_ids),
                Fish.deleted_at.is_(None),
            )
        )
        fish_list = fish_result.scalars().all()
        export_data["fish"] = [
            {
                "id": str(f.id),
                "aquarium_id": str(f.aquarium_id),
                "species_id": f.species_id,
                "quantity": f.quantity,
                "custom_name": f.custom_name,
                "added_via": f.added_via,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in fish_list
        ]

        # Feeding schedules
        schedules_result = await db.execute(
            select(FeedingSchedule).where(FeedingSchedule.aquarium_id.in_(aquarium_ids))
        )
        schedules = schedules_result.scalars().all()
        export_data["feeding_schedules"] = [
            {
                "id": str(s.id),
                "aquarium_id": str(s.aquarium_id),
                "fish_id": str(s.fish_id),
                "time": s.time.strftime("%H:%M") if s.time else None,
                "interval_days": s.interval_days,
                "anchor_date": s.anchor_date.isoformat() if s.anchor_date else None,
                "food_type": s.food_type,
                "portion_hint": s.portion_hint,
                "active": s.active,
                "created_by_user_id": str(s.created_by_user_id) if s.created_by_user_id else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in schedules
        ]

        # Feeding logs
        logs_result = await db.execute(
            select(FeedingLog).where(FeedingLog.aquarium_id.in_(aquarium_ids))
        )
        logs = logs_result.scalars().all()
        export_data["feeding_logs"] = [
            {
                "id": str(log.id),
                "aquarium_id": str(log.aquarium_id),
                "schedule_id": str(log.schedule_id),
                "fish_id": str(log.fish_id),
                "scheduled_for": log.scheduled_for.isoformat() if log.scheduled_for else None,
                "action": log.action,
                "acted_at": log.acted_at.isoformat() if log.acted_at else None,
                "acted_by_user_id": str(log.acted_by_user_id),
            }
            for log in logs
        ]
    else:
        export_data["fish"] = []
        export_data["feeding_schedules"] = []
        export_data["feeding_logs"] = []

    # Streaks
    streak_result = await db.execute(
        select(Streak).where(Streak.user_id == user_id)
    )
    streak = streak_result.scalar_one_or_none()
    if streak:
        export_data["streak"] = {
            "current_streak": streak.current_streak,
            "best_streak": streak.best_streak,
            "freeze_available": streak.freeze_available,
            "last_feed_date": (
                streak.last_feed_date.isoformat() if streak.last_feed_date else None
            ),
        }
    else:
        export_data["streak"] = None

    # Achievements
    achievements_result = await db.execute(
        select(Achievement).where(Achievement.user_id == user_id)
    )
    achievements = achievements_result.scalars().all()
    export_data["achievements"] = [
        {
            "achievement_type": a.achievement_type,
            "unlocked_at": a.unlocked_at.isoformat() if a.unlocked_at else None,
            "shared_at": a.shared_at.isoformat() if a.shared_at else None,
        }
        for a in achievements
    ]

    # AI Scans
    scans_result = await db.execute(
        select(AIScan).where(AIScan.user_id == user_id)
    )
    scans = scans_result.scalars().all()
    export_data["ai_scans"] = [
        {
            "id": str(s.id),
            "detected_species_id": s.detected_species_id,
            "confidence": s.confidence,
            "confirmed_species_id": s.confirmed_species_id,
            "was_corrected": s.was_corrected,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in scans
    ]

    # Analytics events
    analytics_result = await db.execute(
        select(AnalyticsEvent).where(AnalyticsEvent.user_id == user_id)
    )
    analytics_events = analytics_result.scalars().all()
    export_data["analytics_events"] = [
        {
            "id": str(e.id),
            "event_type": e.event_type,
            "properties": e.properties,
            "device_info": e.device_info,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in analytics_events
    ]

    # Push tokens
    tokens_result = await db.execute(
        select(PushToken).where(PushToken.user_id == user_id)
    )
    tokens = tokens_result.scalars().all()
    export_data["push_tokens"] = [
        {
            "platform": t.platform,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tokens
    ]

    # Notification preferences
    prefs_result = await db.execute(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    prefs = prefs_result.scalar_one_or_none()
    if prefs:
        export_data["notification_preferences"] = {
            "global_opt_out": prefs.global_opt_out,
            "timezone": prefs.timezone,
            "feeding_reminders": prefs.feeding_reminders,
            "overdue_alerts": prefs.overdue_alerts,
            "streak_protection": prefs.streak_protection,
            "weekly_summary": prefs.weekly_summary,
            "family_updates": prefs.family_updates,
            "marketing": prefs.marketing,
        }
    else:
        export_data["notification_preferences"] = None

    # Family invites created by user
    invites_result = await db.execute(
        select(FamilyInvite).where(FamilyInvite.created_by == user_id)
    )
    invites = invites_result.scalars().all()
    export_data["family_invites_created"] = [
        {
            "id": str(i.id),
            "aquarium_id": str(i.aquarium_id),
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            "used_at": i.used_at.isoformat() if i.used_at else None,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }
        for i in invites
    ]

    return export_data


async def delete_user_data(db: AsyncSession, user_id: UUID) -> None:
    """Hard delete all user data from all tables.

    Performs complete removal of user data for GDPR compliance.
    Order of deletion respects foreign key constraints.

    Args:
        db: Database session.
        user_id: User ID to delete.

    Raises:
        UserNotFoundError: If user does not exist.
        GDPRError: If deletion fails.
    """
    # Verify user exists
    user_result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        raise UserNotFoundError(user_id)

    logger.info(f"Starting GDPR data deletion for user {user_id}")

    try:
        # Get user's owned aquarium IDs for cascade operations
        aquariums_result = await db.execute(
            select(Aquarium.id).where(Aquarium.owner_id == user_id)
        )
        owned_aquarium_ids = [row[0] for row in aquariums_result.fetchall()]

        # 1. Delete analytics_events (SET NULL would leave orphans, so delete)
        await db.execute(
            delete(AnalyticsEvent).where(AnalyticsEvent.user_id == user_id)
        )
        logger.debug(f"Deleted analytics_events for user {user_id}")

        # 2. Delete ai_scans
        await db.execute(
            delete(AIScan).where(AIScan.user_id == user_id)
        )
        logger.debug(f"Deleted ai_scans for user {user_id}")

        # 3. Delete notification_logs
        await db.execute(
            delete(NotificationLog).where(NotificationLog.user_id == user_id)
        )
        logger.debug(f"Deleted notification_logs for user {user_id}")

        # 4. Delete push_tokens and notification_preferences
        await db.execute(
            delete(PushToken).where(PushToken.user_id == user_id)
        )
        await db.execute(
            delete(NotificationPreference).where(NotificationPreference.user_id == user_id)
        )
        logger.debug(f"Deleted push_tokens and notification_preferences for user {user_id}")

        # 5. Delete streaks and achievements
        await db.execute(
            delete(Streak).where(Streak.user_id == user_id)
        )
        await db.execute(
            delete(Achievement).where(Achievement.user_id == user_id)
        )
        logger.debug(f"Deleted streaks and achievements for user {user_id}")

        # 6. Delete feeding_logs and feeding_schedules from owned aquariums
        if owned_aquarium_ids:
            await db.execute(
                delete(FeedingLog).where(FeedingLog.aquarium_id.in_(owned_aquarium_ids))
            )
            await db.execute(
                delete(FeedingSchedule).where(FeedingSchedule.aquarium_id.in_(owned_aquarium_ids))
            )
            logger.debug(f"Deleted feeding data for owned aquariums of user {user_id}")

            # 8. Delete fish from owned aquariums
            await db.execute(
                delete(Fish).where(Fish.aquarium_id.in_(owned_aquarium_ids))
            )
            logger.debug(f"Deleted fish for owned aquariums of user {user_id}")

        # 9. Delete family_invites (created by user or used by user)
        await db.execute(
            delete(FamilyInvite).where(
                (FamilyInvite.created_by == user_id) | (FamilyInvite.used_by == user_id)
            )
        )
        logger.debug(f"Deleted family_invites for user {user_id}")

        # 10. Delete aquarium_members for this user
        await db.execute(
            delete(AquariumMember).where(AquariumMember.user_id == user_id)
        )
        logger.debug(f"Deleted aquarium_memberships for user {user_id}")

        # 11. Handle orphan aquariums (owned aquariums with no remaining members)
        for aquarium_id in owned_aquarium_ids:
            # Check if aquarium has other members
            members_result = await db.execute(
                select(AquariumMember).where(AquariumMember.aquarium_id == aquarium_id)
            )
            remaining_members = members_result.scalars().all()

            if not remaining_members:
                # No members left, delete the aquarium entirely
                # First delete any remaining family invites for this aquarium
                await db.execute(
                    delete(FamilyInvite).where(FamilyInvite.aquarium_id == aquarium_id)
                )
                # Then delete the aquarium
                await db.execute(
                    delete(Aquarium).where(Aquarium.id == aquarium_id)
                )
                logger.debug(f"Deleted orphan aquarium {aquarium_id}")
            else:
                # Transfer ownership to first remaining member or just delete the aquarium
                # For GDPR compliance, we delete the aquarium as it belongs to the deleted user
                await db.execute(
                    delete(FamilyInvite).where(FamilyInvite.aquarium_id == aquarium_id)
                )
                await db.execute(
                    delete(AquariumMember).where(AquariumMember.aquarium_id == aquarium_id)
                )
                await db.execute(
                    delete(Aquarium).where(Aquarium.id == aquarium_id)
                )
                logger.debug(f"Deleted aquarium {aquarium_id} with remaining members")

        # 12. Delete refresh_tokens
        await db.execute(
            delete(RefreshToken).where(RefreshToken.user_id == user_id)
        )
        logger.debug(f"Deleted refresh_tokens for user {user_id}")

        # 13. Finally, delete the user
        await db.execute(
            delete(User).where(User.id == user_id)
        )
        logger.debug(f"Deleted user record for {user_id}")

        await db.commit()
        logger.info(f"GDPR data deletion completed for user {user_id}")

    except Exception as e:
        await db.rollback()
        logger.error(f"GDPR deletion failed for user {user_id}: {e}")
        raise GDPRError(f"Failed to delete user data: {e}") from None
