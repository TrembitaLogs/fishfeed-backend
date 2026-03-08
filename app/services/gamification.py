"""Gamification service for streak tracking, freeze management, and achievements."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, timedelta
from datetime import datetime as dt
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak
from app.schemas.gamification import AchievementType

logger = structlog.get_logger(__name__)


# ============================================================================
# Achievement Definitions
# ============================================================================


@dataclass
class AchievementDefinition:
    """Definition of an achievement with its trigger condition."""

    achievement_type: AchievementType
    description: str
    check_condition: Callable[[UserStats], bool]


@dataclass
class UserStats:
    """Aggregated user statistics for achievement checking."""

    current_streak: int = 0
    best_streak: int = 0
    total_feedings: int = 0
    fish_count: int = 0
    unique_species_count: int = 0
    aquarium_count: int = 0
    family_members_count: int = 0
    has_early_bird_feeding: bool = False
    has_night_owl_feeding: bool = False
    has_shared_achievement: bool = False


ACHIEVEMENT_DEFINITIONS: list[AchievementDefinition] = [
    # Feeding achievements
    AchievementDefinition(
        achievement_type=AchievementType.FIRST_FEED,
        description="Complete your first feeding",
        check_condition=lambda stats: stats.total_feedings >= 1,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.STREAK_7,
        description="Maintain a 7-day feeding streak",
        check_condition=lambda stats: stats.best_streak >= 7,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.STREAK_30,
        description="Maintain a 30-day feeding streak",
        check_condition=lambda stats: stats.best_streak >= 30,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.STREAK_100,
        description="Maintain a 100-day feeding streak",
        check_condition=lambda stats: stats.best_streak >= 100,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.STREAK_365,
        description="Maintain a 365-day feeding streak",
        check_condition=lambda stats: stats.best_streak >= 365,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.PERFECT_WEEK,
        description="Feed your fish every day for a week",
        check_condition=lambda stats: stats.best_streak >= 7,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.EARLY_BIRD,
        description="Feed your fish before 7 AM",
        check_condition=lambda stats: stats.has_early_bird_feeding,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.NIGHT_OWL,
        description="Feed your fish after 10 PM",
        check_condition=lambda stats: stats.has_night_owl_feeding,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.FEEDING_50,
        description="Complete 50 feedings",
        check_condition=lambda stats: stats.total_feedings >= 50,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.FEEDING_100,
        description="Complete 100 feedings",
        check_condition=lambda stats: stats.total_feedings >= 100,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.FEEDING_500,
        description="Complete 500 feedings",
        check_condition=lambda stats: stats.total_feedings >= 500,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.FEEDING_1000,
        description="Complete 1000 feedings",
        check_condition=lambda stats: stats.total_feedings >= 1000,
    ),
    # Fish achievements
    AchievementDefinition(
        achievement_type=AchievementType.FIRST_FISH,
        description="Add your first fish",
        check_condition=lambda stats: stats.fish_count >= 1,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.FISH_COLLECTOR_10,
        description="Own 10 fish",
        check_condition=lambda stats: stats.fish_count >= 10,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.FISH_COLLECTOR_50,
        description="Own 50 fish",
        check_condition=lambda stats: stats.fish_count >= 50,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.SPECIES_EXPLORER_5,
        description="Own 5 different species",
        check_condition=lambda stats: stats.unique_species_count >= 5,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.SPECIES_EXPLORER_10,
        description="Own 10 different species",
        check_condition=lambda stats: stats.unique_species_count >= 10,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.SPECIES_EXPLORER_20,
        description="Own 20 different species",
        check_condition=lambda stats: stats.unique_species_count >= 20,
    ),
    # Aquarium achievements
    AchievementDefinition(
        achievement_type=AchievementType.FIRST_AQUARIUM,
        description="Create your first aquarium",
        check_condition=lambda stats: stats.aquarium_count >= 1,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.AQUARIUM_COLLECTOR_3,
        description="Own 3 aquariums",
        check_condition=lambda stats: stats.aquarium_count >= 3,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.AQUARIUM_COLLECTOR_10,
        description="Own 10 aquariums",
        check_condition=lambda stats: stats.aquarium_count >= 10,
    ),
    # Family achievements
    AchievementDefinition(
        achievement_type=AchievementType.FAMILY_FIRST,
        description="Add your first family member",
        check_condition=lambda stats: stats.family_members_count >= 1,
    ),
    AchievementDefinition(
        achievement_type=AchievementType.FAMILY_TEAM_3,
        description="Have 3 family members helping",
        check_condition=lambda stats: stats.family_members_count >= 3,
    ),
    # Social achievements
    AchievementDefinition(
        achievement_type=AchievementType.FIRST_SHARE,
        description="Share your first achievement",
        check_condition=lambda stats: stats.has_shared_achievement,
    ),
]


class GamificationError(Exception):
    """Base exception for gamification errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class NoFreezeAvailableError(GamificationError):
    """Raised when user has no freeze days available."""

    def __init__(self) -> None:
        super().__init__("No freeze days available", status_code=400)


class StreakNotFoundError(GamificationError):
    """Raised when streak record is not found."""

    def __init__(self, user_id: UUID):
        super().__init__(f"Streak not found for user '{user_id}'", status_code=404)


async def get_or_create_streak(db: AsyncSession, user_id: UUID) -> Streak:
    """Get existing streak record or create a new one.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        Streak record for the user.
    """
    stmt = select(Streak).where(Streak.user_id == user_id)
    result = await db.execute(stmt)
    streak = result.scalar_one_or_none()

    if streak is None:
        streak = Streak(user_id=user_id)
        db.add(streak)
        await db.flush()
        await db.refresh(streak)
        logger.info(f"Created new streak record for user '{user_id}'")

    return streak


async def update_streak(db: AsyncSession, user_id: UUID) -> Streak:
    """Update user streak after a feeding event.

    Logic:
    - If last_feed_date == today: do nothing (already fed today)
    - If last_feed_date == yesterday: increment streak
    - If last_feed_date is older or None: reset streak to 1

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        Updated Streak record.
    """
    streak = await get_or_create_streak(db, user_id)
    today = dt.now(UTC).date()
    yesterday = today - timedelta(days=1)

    if streak.last_feed_date == today:
        logger.debug(f"User '{user_id}' already fed today, streak unchanged")
        return streak

    if streak.last_feed_date == yesterday:
        streak.current_streak += 1
        logger.info(
            f"User '{user_id}' streak incremented to {streak.current_streak}"
        )
    else:
        streak.current_streak = 1
        if streak.last_feed_date is not None:
            logger.info(
                f"User '{user_id}' streak reset to 1 "
                f"(last feed: {streak.last_feed_date})"
            )
        else:
            logger.info(f"User '{user_id}' started first streak")

    if streak.current_streak > streak.best_streak:
        streak.best_streak = streak.current_streak
        logger.info(f"User '{user_id}' new best streak: {streak.best_streak}")

    streak.last_feed_date = today

    await db.flush()
    await db.refresh(streak)

    return streak


async def use_freeze(db: AsyncSession, user_id: UUID) -> bool:
    """Use a freeze day to preserve streak.

    Freeze prevents streak reset when user misses a feeding day.
    User must have freeze_available > 0.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        True if freeze was successfully used, False if no freeze available.

    Raises:
        StreakNotFoundError: If streak record doesn't exist.
    """
    stmt = select(Streak).where(Streak.user_id == user_id)
    result = await db.execute(stmt)
    streak = result.scalar_one_or_none()

    if streak is None:
        raise StreakNotFoundError(user_id)

    if streak.freeze_available <= 0:
        logger.warning(f"User '{user_id}' attempted to use freeze but none available")
        return False

    streak.freeze_available -= 1
    streak.freeze_used_this_period += 1

    today = dt.now(UTC).date()

    # Set last_feed_date to today so next day's feeding sees yesterday as last feed
    streak.last_feed_date = today

    await db.flush()
    await db.refresh(streak)

    logger.info(
        f"User '{user_id}' used freeze day, {streak.freeze_available} remaining"
    )

    return True


async def get_streak(db: AsyncSession, user_id: UUID) -> Streak | None:
    """Get streak record for user.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        Streak record or None if not found.
    """
    stmt = select(Streak).where(Streak.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ============================================================================
# Achievement Functions
# ============================================================================


async def _get_user_stats(db: AsyncSession, user_id: UUID) -> UserStats:
    """Collect aggregated statistics for achievement checking.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        UserStats with all relevant statistics.
    """
    stats = UserStats()

    # Get streak data
    streak = await get_streak(db, user_id)
    if streak:
        stats.current_streak = streak.current_streak
        stats.best_streak = streak.best_streak

    # Get owned aquariums (only non-deleted)
    aquarium_stmt = select(Aquarium).where(
        Aquarium.owner_id == user_id,
        Aquarium.deleted_at.is_(None),
    )
    aquarium_result = await db.execute(aquarium_stmt)
    aquariums = aquarium_result.scalars().all()
    stats.aquarium_count = len(aquariums)
    aquarium_ids = [a.id for a in aquariums]

    if aquarium_ids:
        # Get fish count (sum of quantities, only non-deleted fish in owned aquariums)
        fish_count_stmt = select(func.coalesce(func.sum(Fish.quantity), 0)).where(
            Fish.aquarium_id.in_(aquarium_ids),
            Fish.deleted_at.is_(None),
        )
        fish_result = await db.execute(fish_count_stmt)
        stats.fish_count = int(fish_result.scalar_one())

        # Get unique species count
        species_stmt = select(func.count(func.distinct(Fish.species_id))).where(
            Fish.aquarium_id.in_(aquarium_ids),
            Fish.deleted_at.is_(None),
        )
        species_result = await db.execute(species_stmt)
        stats.unique_species_count = int(species_result.scalar_one())

        # Get family members count (excluding the owner themselves)
        family_stmt = select(func.count(AquariumMember.user_id)).where(
            AquariumMember.aquarium_id.in_(aquarium_ids),
            AquariumMember.user_id != user_id,
        )
        family_result = await db.execute(family_stmt)
        stats.family_members_count = int(family_result.scalar_one())

        # Get total completed feedings and time-based achievements
        feeding_stmt = select(FeedingLog).where(
            FeedingLog.aquarium_id.in_(aquarium_ids),
            FeedingLog.action == "fed",
            FeedingLog.acted_by_user_id == user_id,
        )
        feeding_result = await db.execute(feeding_stmt)
        feedings: list[FeedingLog] = list(feeding_result.scalars().all())
        stats.total_feedings = len(feedings)

        # Check for early bird (before 7 AM) and night owl (after 10 PM)
        for feeding in feedings:
            hour = feeding.acted_at.hour
            if hour < 7:
                stats.has_early_bird_feeding = True
            if hour >= 22:
                stats.has_night_owl_feeding = True
            if stats.has_early_bird_feeding and stats.has_night_owl_feeding:
                break

    # Check for shared achievements
    shared_stmt = select(Achievement).where(
        Achievement.user_id == user_id,
        Achievement.shared_at.is_not(None),
    )
    result = await db.execute(shared_stmt)
    shared = result.scalars().first()
    stats.has_shared_achievement = shared is not None

    return stats


async def _get_unlocked_achievement_types(
    db: AsyncSession, user_id: UUID
) -> set[AchievementType]:
    """Get set of already unlocked achievement types for user.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        Set of AchievementType values that are already unlocked.
    """
    stmt = select(Achievement.achievement_type).where(Achievement.user_id == user_id)
    result = await db.execute(stmt)
    return {AchievementType(t) for t in result.scalars().all()}


async def check_achievements(db: AsyncSession, user_id: UUID) -> list[Achievement]:
    """Check and unlock new achievements for user.

    Evaluates all achievement conditions and creates records for newly
    unlocked achievements. Skips already unlocked achievements to avoid
    duplicates.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        List of newly unlocked Achievement records.
    """
    # Get current stats and already unlocked achievements
    stats = await _get_user_stats(db, user_id)
    unlocked_types = await _get_unlocked_achievement_types(db, user_id)

    newly_unlocked: list[Achievement] = []

    for definition in ACHIEVEMENT_DEFINITIONS:
        # Skip if already unlocked
        if definition.achievement_type in unlocked_types:
            continue

        # Check if condition is met
        if definition.check_condition(stats):
            achievement = Achievement(
                user_id=user_id,
                achievement_type=definition.achievement_type.value,
            )
            db.add(achievement)
            newly_unlocked.append(achievement)
            logger.info(
                f"User '{user_id}' unlocked achievement: {definition.achievement_type.value}"
            )

    if newly_unlocked:
        await db.flush()
        for achievement in newly_unlocked:
            await db.refresh(achievement)

    return newly_unlocked


async def get_achievements(db: AsyncSession, user_id: UUID) -> list[Achievement]:
    """Get all achievements for user.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        List of Achievement records.
    """
    stmt = select(Achievement).where(Achievement.user_id == user_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def share_achievement(
    db: AsyncSession, user_id: UUID, achievement_id: UUID
) -> Achievement | None:
    """Mark achievement as shared.

    Args:
        db: Database session.
        user_id: User ID.
        achievement_id: Achievement ID to share.

    Returns:
        Updated Achievement or None if not found.
    """
    stmt = select(Achievement).where(
        Achievement.id == achievement_id,
        Achievement.user_id == user_id,
    )
    result = await db.execute(stmt)
    achievement = result.scalar_one_or_none()

    if achievement is None:
        return None

    achievement.shared_at = dt.now(UTC)
    await db.flush()
    await db.refresh(achievement)

    logger.info(
        f"User '{user_id}' shared achievement: {achievement.achievement_type}"
    )

    return achievement


async def get_achievement_by_id(
    db: AsyncSession, achievement_id: UUID
) -> Achievement | None:
    """Get achievement by ID.

    Args:
        db: Database session.
        achievement_id: Achievement ID.

    Returns:
        Achievement or None if not found.
    """
    stmt = select(Achievement).where(Achievement.id == achievement_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


@dataclass
class UserStatsResult:
    """Result of get_user_stats aggregation."""

    streak: Streak
    achievements: list[Achievement]
    total_feedings: int
    fish_count: int


async def get_user_stats(db: AsyncSession, user_id: UUID) -> UserStatsResult:
    """Get aggregated user gamification stats.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        UserStatsResult with streak, achievements, total_feedings, fish_count.
    """
    streak = await get_or_create_streak(db, user_id)
    achievements = await get_achievements(db, user_id)
    internal_stats = await _get_user_stats(db, user_id)

    return UserStatsResult(
        streak=streak,
        achievements=achievements,
        total_feedings=internal_stats.total_feedings,
        fish_count=internal_stats.fish_count,
    )
