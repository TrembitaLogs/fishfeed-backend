"""Notification background jobs for scheduled push notifications.

This module provides scheduled jobs for:
- Weekly summary notifications (Sunday at 10:00 UTC)
- Re-engagement notifications for inactive users (daily)
- Family feeding triggers when a member completes feeding
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_maker
from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog
from app.models.user import User
from app.services.notification import NotificationService

logger = structlog.get_logger(__name__)
settings = get_settings()


async def weekly_summary_job() -> int:
    """Send weekly feeding summary to all active users.

    Collects feeding statistics for the past week and sends a summary
    notification to users who have push tokens registered.

    Runs every Sunday at configured time (default 10:00 UTC).

    Returns:
        Number of notifications sent.
    """
    logger.info("Starting weekly_summary_job")

    now = datetime.now(UTC)
    week_start = now - timedelta(days=7)

    async with async_session_maker() as db:
        # Get all users who have push tokens (active users)
        users_with_tokens_stmt = (
            select(distinct(User.id))
            .join(
                AquariumMember,
                AquariumMember.user_id == User.id,
            )
            .where(User.deleted_at.is_(None))
        )
        result = await db.execute(users_with_tokens_stmt)
        user_ids = [row[0] for row in result.all()]

        if not user_ids:
            logger.info("No active users found for weekly summary")
            return 0

        sent_count = 0
        notification_service = NotificationService(db)

        for user_id in user_ids:
            try:
                stats = await _get_user_weekly_stats(db, user_id, week_start, now)

                if stats["total_events"] == 0:
                    continue

                title = "Weekly Feeding Summary"
                body = _build_summary_message(stats)

                success = await notification_service.send_push(
                    user_id=user_id,
                    title=title,
                    body=body,
                    data={
                        "type": "weekly_summary",
                        "week_start": week_start.isoformat(),
                        "week_end": now.isoformat(),
                        "completed": stats["completed"],
                        "missed": stats["missed"],
                    },
                    notification_type="weekly_summary",
                )

                if success:
                    sent_count += 1

            except Exception as e:
                logger.error(f"Failed to send weekly summary to user {user_id}: {e}")
                continue

        await db.commit()
        logger.info(f"Weekly summary sent to {sent_count} users")
        return sent_count


async def _get_user_weekly_stats(
    db: AsyncSession,
    user_id: UUID,
    week_start: datetime,
    week_end: datetime,
) -> dict:
    """Get feeding statistics for a user's aquariums over a week.

    Args:
        db: Database session.
        user_id: User ID.
        week_start: Start of the week.
        week_end: End of the week.

    Returns:
        Dict with completed, missed, total_events counts.
    """
    # Get all aquariums user has access to
    aquarium_ids_stmt = select(AquariumMember.aquarium_id).where(
        AquariumMember.user_id == user_id
    )
    result = await db.execute(aquarium_ids_stmt)
    aquarium_ids = [row[0] for row in result.all()]

    if not aquarium_ids:
        return {"completed": 0, "missed": 0, "total_events": 0}

    # Count fed logs
    fed_stmt = (
        select(func.count())
        .select_from(FeedingLog)
        .where(FeedingLog.aquarium_id.in_(aquarium_ids))
        .where(FeedingLog.acted_at >= week_start)
        .where(FeedingLog.acted_at <= week_end)
        .where(FeedingLog.action == "fed")
    )
    fed_result = await db.execute(fed_stmt)
    completed = fed_result.scalar_one()

    # Count skipped logs
    skipped_stmt = (
        select(func.count())
        .select_from(FeedingLog)
        .where(FeedingLog.aquarium_id.in_(aquarium_ids))
        .where(FeedingLog.acted_at >= week_start)
        .where(FeedingLog.acted_at <= week_end)
        .where(FeedingLog.action == "skipped")
    )
    skipped_result = await db.execute(skipped_stmt)
    missed = skipped_result.scalar_one()

    return {
        "completed": completed,
        "missed": missed,
        "total_events": completed + missed,
    }


def _build_summary_message(stats: dict) -> str:
    """Build a human-readable summary message.

    Args:
        stats: Dict with completed, missed, total_events counts.

    Returns:
        Summary message string.
    """
    completed = stats["completed"]
    missed = stats["missed"]
    total = stats["total_events"]

    if total == 0:
        return "No feeding events this week."

    completion_rate = (completed / total) * 100 if total > 0 else 0

    if completion_rate == 100:
        return f"Perfect week! All {completed} feedings completed."
    elif completion_rate >= 80:
        return f"Great job! {completed}/{total} feedings completed ({completion_rate:.0f}%)."
    elif completion_rate >= 50:
        return f"Good effort! {completed}/{total} feedings completed. {missed} missed."
    else:
        return f"Your fish need attention! Only {completed}/{total} feedings completed."


async def re_engagement_job() -> int:
    """Send re-engagement push to users inactive for configured days.

    Finds users who have not completed any feeding events within the
    configured inactivity period and sends them a reminder notification.

    Runs daily at configured time (default 12:00 UTC).

    Returns:
        Number of notifications sent.
    """
    logger.info("Starting re_engagement_job")

    inactivity_days = settings.NOTIFICATION_INACTIVITY_DAYS
    now = datetime.now(UTC)
    cutoff_date = now - timedelta(days=inactivity_days)

    async with async_session_maker() as db:
        # Find users with aquarium memberships who haven't completed
        # any feeding events in the last N days
        inactive_users = await _find_inactive_users(db, cutoff_date)

        if not inactive_users:
            logger.info("No inactive users found for re-engagement")
            return 0

        sent_count = 0
        notification_service = NotificationService(db)

        for user_id in inactive_users:
            try:
                success = await notification_service.send_push(
                    user_id=user_id,
                    title="Your fish miss you!",
                    body="It's been a while since your last feeding. Your fish are waiting!",
                    data={
                        "type": "re_engagement",
                        "days_inactive": inactivity_days,
                    },
                    notification_type="feeding_reminder",
                )

                if success:
                    sent_count += 1

            except Exception as e:
                logger.error(f"Failed to send re-engagement to user {user_id}: {e}")
                continue

        await db.commit()
        logger.info(f"Re-engagement sent to {sent_count} users")
        return sent_count


async def _find_inactive_users(
    db: AsyncSession,
    cutoff_date: datetime,
) -> list[UUID]:
    """Find users who have not completed feedings since cutoff date.

    Args:
        db: Database session.
        cutoff_date: Date threshold for inactivity.

    Returns:
        List of inactive user IDs.
    """
    # Subquery: users who have logged a feeding after cutoff
    active_users_subquery = (
        select(distinct(FeedingLog.acted_by_user_id))
        .where(FeedingLog.acted_at >= cutoff_date)
        .where(FeedingLog.action == "fed")
    ).subquery()

    # Main query: users with aquarium memberships who are NOT in active list
    inactive_stmt = (
        select(distinct(AquariumMember.user_id))
        .join(User, AquariumMember.user_id == User.id)
        .where(User.deleted_at.is_(None))
        .where(AquariumMember.user_id.notin_(select(active_users_subquery)))
    )

    result = await db.execute(inactive_stmt)
    return [row[0] for row in result.all()]


async def family_feeding_trigger(
    db: AsyncSession,
    feeding_log_id: UUID,
    completed_by_user_id: UUID,
) -> int:
    """Send push notification to family members when feeding is logged.

    Notifies all members of the aquarium (except the user who logged
    the feeding) that a feeding has been recorded.

    This function is called directly from the feeding service, not scheduled.

    Args:
        db: Database session.
        feeding_log_id: ID of the created feeding log.
        completed_by_user_id: ID of the user who performed the feeding.

    Returns:
        Number of notifications sent.
    """
    logger.info(
        f"Triggering family notification for feeding log {feeding_log_id} "
        f"by user {completed_by_user_id}"
    )

    # Get the feeding log with aquarium info
    log_stmt = select(FeedingLog).where(FeedingLog.id == feeding_log_id)
    result = await db.execute(log_stmt)
    log = result.scalar_one_or_none()

    if log is None:
        logger.warning(f"Feeding log {feeding_log_id} not found")
        return 0

    # Get aquarium name
    aquarium_stmt = select(Aquarium).where(Aquarium.id == log.aquarium_id)
    aquarium_result = await db.execute(aquarium_stmt)
    aquarium = aquarium_result.scalar_one_or_none()

    if aquarium is None:
        logger.warning(f"Aquarium {log.aquarium_id} not found")
        return 0

    # Get the nickname of the user who completed feeding
    user_stmt = select(User.nickname).where(User.id == completed_by_user_id)
    user_result = await db.execute(user_stmt)
    user_nickname = user_result.scalar_one_or_none() or "Someone"

    # Get all family members except the one who did the feeding
    members_stmt = (
        select(AquariumMember.user_id)
        .where(AquariumMember.aquarium_id == log.aquarium_id)
        .where(AquariumMember.user_id != completed_by_user_id)
    )
    members_result = await db.execute(members_stmt)
    member_ids = [row[0] for row in members_result.all()]

    if not member_ids:
        logger.debug(f"No other family members for aquarium {log.aquarium_id}")
        return 0

    notification_service = NotificationService(db)
    sent_count = 0

    title = f"{aquarium.name}: Fish fed!"
    body = f"{user_nickname} just fed the fish."

    for member_id in member_ids:
        try:
            success = await notification_service.send_push(
                user_id=member_id,
                title=title,
                body=body,
                data={
                    "type": "family_feeding",
                    "aquarium_id": str(log.aquarium_id),
                    "feeding_log_id": str(feeding_log_id),
                    "completed_by": str(completed_by_user_id),
                },
                notification_type="family_update",
            )

            if success:
                sent_count += 1

        except Exception as e:
            logger.error(f"Failed to send family notification to {member_id}: {e}")
            continue

    logger.info(
        f"Family feeding notification sent to {sent_count}/{len(member_ids)} members"
    )
    return sent_count
