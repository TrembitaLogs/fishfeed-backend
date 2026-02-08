"""Admin service for dashboard statistics and user management."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIScan
from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.gamification import Achievement, Streak
from app.models.user import User
from app.schemas.admin import (
    DashboardAIStats,
    DashboardAquariumsStats,
    DashboardFeedingStats,
    DashboardGamificationStats,
    DashboardResponse,
    DashboardUsersStats,
)

DEFAULT_FREE_AI_SCANS = 5


async def get_dashboard_stats(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> DashboardResponse:
    """Aggregate statistics from all models for the admin dashboard.

    Args:
        db: Async database session.
        now: Optional override for current time (useful for testing).

    Returns:
        DashboardResponse with aggregated stats across all categories.
    """
    if now is None:
        now = datetime.now(UTC)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days_ago = now - timedelta(days=7)

    users = await _get_users_stats(db, today_start=today_start, seven_days_ago=seven_days_ago)
    aquariums = await _get_aquariums_stats(db)
    feeding = await _get_feeding_stats(db, today_start=today_start)
    ai_scans = await _get_ai_stats(db, today_start=today_start)
    gamification = await _get_gamification_stats(db, today_start=today_start)

    return DashboardResponse(
        users=users,
        aquariums=aquariums,
        feeding=feeding,
        ai_scans=ai_scans,
        gamification=gamification,
    )


async def _get_users_stats(
    db: AsyncSession,
    *,
    today_start: datetime,
    seven_days_ago: datetime,
) -> DashboardUsersStats:
    """Aggregate user-related statistics."""
    # Total non-deleted users
    total_result = await db.execute(
        select(func.count()).select_from(User).where(User.deleted_at.is_(None))
    )
    total = total_result.scalar_one()

    # Active in last 7 days: distinct users who have feeding logs
    active_result = await db.execute(
        select(func.count(func.distinct(FeedingLog.acted_by_user_id))).where(
            FeedingLog.acted_at >= seven_days_ago
        )
    )
    active_last_7d = active_result.scalar_one()

    # Premium users
    premium_result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.deleted_at.is_(None), User.subscription_status == "premium")
    )
    premium = premium_result.scalar_one()

    # New users today
    new_today_result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.deleted_at.is_(None), User.created_at >= today_start)
    )
    new_today = new_today_result.scalar_one()

    return DashboardUsersStats(
        total=total,
        active_last_7d=active_last_7d,
        premium=premium,
        new_today=new_today,
    )


async def _get_aquariums_stats(db: AsyncSession) -> DashboardAquariumsStats:
    """Aggregate aquarium-related statistics."""
    # Total non-deleted aquariums
    total_result = await db.execute(
        select(func.count()).select_from(Aquarium).where(Aquarium.deleted_at.is_(None))
    )
    total = total_result.scalar_one()

    # Aquariums with at least one family member
    family_subquery = (
        select(AquariumMember.aquarium_id).distinct().correlate(None).scalar_subquery()
    )
    family_result = await db.execute(
        select(func.count())
        .select_from(Aquarium)
        .where(Aquarium.deleted_at.is_(None), Aquarium.id.in_(family_subquery))
    )
    with_family_members = family_result.scalar_one()

    return DashboardAquariumsStats(
        total=total,
        with_family_members=with_family_members,
    )


async def _get_feeding_stats(
    db: AsyncSession,
    *,
    today_start: datetime,
) -> DashboardFeedingStats:
    """Aggregate feeding-related statistics."""
    # Feeding logs today
    logs_today_result = await db.execute(
        select(func.count()).select_from(FeedingLog).where(
            FeedingLog.acted_at >= today_start
        )
    )
    logs_today = logs_today_result.scalar_one()

    # Active feeding schedules
    active_result = await db.execute(
        select(func.count()).select_from(FeedingSchedule).where(
            FeedingSchedule.active.is_(True)
        )
    )
    schedules_active = active_result.scalar_one()

    return DashboardFeedingStats(
        logs_today=logs_today,
        schedules_active=schedules_active,
    )


async def _get_ai_stats(
    db: AsyncSession,
    *,
    today_start: datetime,
) -> DashboardAIStats:
    """Aggregate AI scan statistics."""
    # Total AI scans
    total_result = await db.execute(
        select(func.count()).select_from(AIScan)
    )
    total = total_result.scalar_one()

    # AI scans today
    today_result = await db.execute(
        select(func.count()).select_from(AIScan).where(
            AIScan.created_at >= today_start
        )
    )
    today = today_result.scalar_one()

    return DashboardAIStats(total=total, today=today)


async def _get_gamification_stats(
    db: AsyncSession,
    *,
    today_start: datetime,
) -> DashboardGamificationStats:
    """Aggregate gamification statistics."""
    # Average and max streak
    streak_result = await db.execute(
        select(
            func.coalesce(func.avg(Streak.current_streak), 0),
            func.coalesce(func.max(Streak.current_streak), 0),
        )
    )
    row = streak_result.one()
    avg_streak = float(row[0])
    max_streak = int(row[1])

    # Achievements unlocked today
    achievements_today_result = await db.execute(
        select(func.count()).select_from(Achievement).where(
            Achievement.unlocked_at >= today_start
        )
    )
    achievements_unlocked_today = achievements_today_result.scalar_one()

    return DashboardGamificationStats(
        avg_streak=round(avg_streak, 2),
        max_streak=max_streak,
        achievements_unlocked_today=achievements_unlocked_today,
    )


# ---------------------------------------------------------------------------
# User management helpers
# ---------------------------------------------------------------------------


async def _get_user_or_404(db: AsyncSession, user_id: UUID) -> User:
    """Fetch user by ID or raise 404."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


async def _invalidate_refresh_tokens(redis: Redis, user_id: UUID) -> None:
    """Delete all refresh tokens for a user from Redis.

    Scans Redis keys matching ``refresh:*`` and removes those whose stored
    value equals the given *user_id*.
    """
    user_id_str = str(user_id)
    cursor: int = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match="refresh:*", count=200)
        if keys:
            pipe = redis.pipeline()
            for key in keys:
                pipe.get(key)
            values = await pipe.execute()

            to_delete = [k for k, v in zip(keys, values, strict=False) if v == user_id_str]
            if to_delete:
                await redis.delete(*to_delete)

        if not cursor:
            break


async def ban_user(db: AsyncSession, redis: Redis, user_id: UUID) -> None:
    """Ban a user by soft-deleting and revoking all refresh tokens.

    Args:
        db: Database session.
        redis: Redis client.
        user_id: Target user UUID.

    Raises:
        HTTPException: 404 if user not found.
    """
    user = await _get_user_or_404(db, user_id)
    user.deleted_at = datetime.now(UTC)
    await db.flush()
    await _invalidate_refresh_tokens(redis, user_id)


async def unban_user(db: AsyncSession, user_id: UUID) -> None:
    """Unban a user by clearing the soft-delete timestamp.

    Args:
        db: Database session.
        user_id: Target user UUID.

    Raises:
        HTTPException: 404 if user not found.
    """
    user = await _get_user_or_404(db, user_id)
    user.deleted_at = None
    await db.flush()


async def reset_ai_scans(db: AsyncSession, user_id: UUID) -> None:
    """Reset a user's free AI scan quota to the default value.

    Args:
        db: Database session.
        user_id: Target user UUID.

    Raises:
        HTTPException: 404 if user not found.
    """
    user = await _get_user_or_404(db, user_id)
    user.free_ai_scans_remaining = DEFAULT_FREE_AI_SCANS
    await db.flush()


async def grant_premium(db: AsyncSession, user_id: UUID, days: int) -> None:
    """Grant premium subscription to a user for a given number of days.

    Args:
        db: Database session.
        user_id: Target user UUID.
        days: Number of days of premium subscription.

    Raises:
        HTTPException: 404 if user not found.
    """
    user = await _get_user_or_404(db, user_id)
    user.subscription_status = "premium"
    user.subscription_expires_at = datetime.now(UTC) + timedelta(days=days)
    await db.flush()


async def update_subscription(
    db: AsyncSession,
    user_id: UUID,
    status: str,
    expires_at: datetime | None,
) -> User:
    """Update a user's subscription status and expiration.

    Args:
        db: Database session.
        user_id: Target user UUID.
        status: New subscription status ('free', 'premium', or 'expired').
        expires_at: New expiration datetime, or None to clear.

    Returns:
        Updated User object.

    Raises:
        HTTPException: 404 if user not found.
    """
    user = await _get_user_or_404(db, user_id)
    user.subscription_status = status
    user.subscription_expires_at = expires_at
    await db.flush()
    await db.refresh(user)
    return user
