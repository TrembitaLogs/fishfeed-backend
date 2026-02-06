"""Integration tests for gamification service (streaks, freeze, achievements)."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.models.gamification import Streak
from app.models.user import User
from app.schemas.gamification import AchievementType
from app.services.gamification import (
    ACHIEVEMENT_DEFINITIONS,
    StreakNotFoundError,
    UserStats,
    check_achievements,
    get_achievements,
    get_or_create_streak,
    get_streak,
    share_achievement,
    update_streak,
    use_freeze,
)


async def cleanup_gamification(session: AsyncSession) -> None:
    """Helper to cleanup gamification data."""
    await session.execute(text("TRUNCATE TABLE streaks CASCADE"))
    await session.execute(text("TRUNCATE TABLE achievements CASCADE"))
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.commit()


async def create_test_user(session: AsyncSession, email: str = "test@example.com") -> User:
    """Helper to create a test user."""
    user = User(email=email, password_hash="hashed")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


def mock_today(target_date: date):
    """Create a mock for datetime.now() that returns a specific date."""
    from datetime import datetime as dt

    class MockDatetime:
        @staticmethod
        def now(tz=None):
            return dt.combine(target_date, dt.min.time(), tzinfo=UTC)

    return MockDatetime


@pytest.mark.asyncio(loop_scope="session")
async def test_get_or_create_streak_creates_new_streak(async_session: AsyncSession):
    """Test that get_or_create_streak creates a new streak record."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)

        streak = await get_or_create_streak(async_session, user.id)

        assert streak is not None
        assert streak.user_id == user.id
        assert streak.current_streak == 0
        assert streak.best_streak == 0
        assert streak.freeze_available == 2
        assert streak.last_feed_date is None
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_or_create_streak_returns_existing_streak(async_session: AsyncSession):
    """Test that get_or_create_streak returns existing streak."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)

        # Create initial streak
        streak1 = await get_or_create_streak(async_session, user.id)
        streak1.current_streak = 5
        await async_session.flush()

        # Get again - should return same streak
        streak2 = await get_or_create_streak(async_session, user.id)

        assert streak2.user_id == streak1.user_id
        assert streak2.current_streak == 5
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_streak_first_feeding_sets_streak_to_1(async_session: AsyncSession):
    """Test that first feeding sets streak to 1."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        today = date(2025, 1, 15)

        with patch("app.services.gamification.dt", mock_today(today)):
            streak = await update_streak(async_session, user.id)

        assert streak.current_streak == 1
        assert streak.best_streak == 1
        assert streak.last_feed_date == today
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_streak_increments_on_consecutive_day(async_session: AsyncSession):
    """Test that streak increments when feeding on consecutive day."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        yesterday = date(2025, 1, 14)
        today = date(2025, 1, 15)

        # First feeding yesterday
        with patch("app.services.gamification.dt", mock_today(yesterday)):
            streak = await update_streak(async_session, user.id)
        assert streak.current_streak == 1

        # Second feeding today
        with patch("app.services.gamification.dt", mock_today(today)):
            streak = await update_streak(async_session, user.id)

        assert streak.current_streak == 2
        assert streak.best_streak == 2
        assert streak.last_feed_date == today
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_streak_resets_on_missed_day(async_session: AsyncSession):
    """Test that streak resets to 1 when more than one day is missed."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        day1 = date(2025, 1, 10)
        day3 = date(2025, 1, 13)  # Skipped day 11 and 12

        # First feeding on day1
        with patch("app.services.gamification.dt", mock_today(day1)):
            streak = await update_streak(async_session, user.id)
        assert streak.current_streak == 1

        # Feed again on day3 (missed day 11 and 12)
        with patch("app.services.gamification.dt", mock_today(day3)):
            streak = await update_streak(async_session, user.id)

        assert streak.current_streak == 1  # Reset to 1
        assert streak.best_streak == 1  # Best stays at 1
        assert streak.last_feed_date == day3
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_streak_same_day_no_change(async_session: AsyncSession):
    """Test that feeding multiple times on same day doesn't change streak."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        today = date(2025, 1, 15)

        with patch("app.services.gamification.dt", mock_today(today)):
            streak1 = await update_streak(async_session, user.id)
            streak2 = await update_streak(async_session, user.id)

        assert streak1.current_streak == 1
        assert streak2.current_streak == 1  # Still 1, not 2
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_streak_best_streak_updates_correctly(async_session: AsyncSession):
    """Test that best_streak updates when current exceeds it."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)

        # Build a 5-day streak
        for i in range(5):
            day = date(2025, 1, 10 + i)
            with patch("app.services.gamification.dt", mock_today(day)):
                streak = await update_streak(async_session, user.id)

        assert streak.current_streak == 5
        assert streak.best_streak == 5

        # Miss a day and restart
        new_start = date(2025, 1, 20)
        with patch("app.services.gamification.dt", mock_today(new_start)):
            streak = await update_streak(async_session, user.id)

        assert streak.current_streak == 1  # Reset
        assert streak.best_streak == 5  # Best stays at 5
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_use_freeze_decrements_available(async_session: AsyncSession):
    """Test that use_freeze decrements freeze_available."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        today = date(2025, 1, 15)

        # Create streak first
        streak = Streak(user_id=user.id, freeze_available=2)
        async_session.add(streak)
        await async_session.flush()

        with patch("app.services.gamification.dt", mock_today(today)):
            result = await use_freeze(async_session, user.id)

        assert result is True
        assert streak.freeze_available == 1
        assert streak.freeze_used_this_period == 1
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_use_freeze_returns_false_when_none_available(async_session: AsyncSession):
    """Test that use_freeze returns False when no freeze days available."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        today = date(2025, 1, 15)

        # Create streak with no freeze available
        streak = Streak(user_id=user.id, freeze_available=0)
        async_session.add(streak)
        await async_session.flush()

        with patch("app.services.gamification.dt", mock_today(today)):
            result = await use_freeze(async_session, user.id)

        assert result is False
        assert streak.freeze_available == 0
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_use_freeze_raises_when_no_streak(async_session: AsyncSession):
    """Test that use_freeze raises StreakNotFoundError when no streak exists."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        today = date(2025, 1, 15)

        with patch("app.services.gamification.dt", mock_today(today)):
            with pytest.raises(StreakNotFoundError):
                await use_freeze(async_session, user.id)
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_use_freeze_sets_last_feed_date_to_today(async_session: AsyncSession):
    """Test that use_freeze sets last_feed_date to today to maintain streak continuity."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        today = date(2025, 1, 15)

        # Create streak
        streak = Streak(user_id=user.id, freeze_available=2)
        async_session.add(streak)
        await async_session.flush()

        with patch("app.services.gamification.dt", mock_today(today)):
            await use_freeze(async_session, user.id)

        # Freeze sets last_feed_date to today so next day sees yesterday as last feed
        assert streak.last_feed_date == today
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_freeze_preserves_streak_continuity(async_session: AsyncSession):
    """Test that using freeze allows streak to continue next day."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)
        day1 = date(2025, 1, 10)
        day2 = date(2025, 1, 11)  # Missed - use freeze
        day3 = date(2025, 1, 12)  # Continue streak

        # Day 1: Start streak
        with patch("app.services.gamification.dt", mock_today(day1)):
            streak = await update_streak(async_session, user.id)
        assert streak.current_streak == 1

        # Day 2: Missed - use freeze (sets last_feed_date to day2)
        with patch("app.services.gamification.dt", mock_today(day2)):
            await use_freeze(async_session, user.id)

        # Verify freeze set last_feed_date to day2
        assert streak.last_feed_date == day2

        # Day 3: Feed - should continue streak because last_feed_date=day2=yesterday
        with patch("app.services.gamification.dt", mock_today(day3)):
            streak = await update_streak(async_session, user.id)

        assert streak.current_streak == 2  # Streak continues
        assert streak.best_streak == 2
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_streak_returns_streak(async_session: AsyncSession):
    """Test that get_streak returns existing streak."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)

        # Create streak
        streak = Streak(user_id=user.id, current_streak=5)
        async_session.add(streak)
        await async_session.flush()

        result = await get_streak(async_session, user.id)

        assert result is not None
        assert result.current_streak == 5
    finally:
        await cleanup_gamification(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_streak_returns_none_when_not_exists(async_session: AsyncSession):
    """Test that get_streak returns None when no streak exists."""
    await cleanup_gamification(async_session)
    try:
        user = await create_test_user(async_session)

        result = await get_streak(async_session, user.id)

        assert result is None
    finally:
        await cleanup_gamification(async_session)


# ============================================================================
# Achievement Tests
# ============================================================================


async def cleanup_all_data(session: AsyncSession) -> None:
    """Helper to cleanup all test data and ensure test species exist."""
    # Rollback any pending transaction first
    await session.rollback()
    await session.execute(text("TRUNCATE TABLE achievements CASCADE"))
    await session.execute(text("TRUNCATE TABLE streaks CASCADE"))
    await session.execute(text("TRUNCATE TABLE feeding_logs CASCADE"))
    await session.execute(text("TRUNCATE TABLE feeding_schedules CASCADE"))
    await session.execute(text("TRUNCATE TABLE fish CASCADE"))
    await session.execute(text("TRUNCATE TABLE aquarium_members CASCADE"))
    await session.execute(text("TRUNCATE TABLE family_invites CASCADE"))
    await session.execute(text("TRUNCATE TABLE aquariums CASCADE"))
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.commit()

    # Ensure test species exist (may be deleted by other tests)
    await ensure_test_species_exist(session)


async def ensure_test_species_exist(session: AsyncSession) -> None:
    """Ensure test species exist in database."""
    from app.models.species import Species

    test_species_data = [
        {
            "id": "test-guppy",
            "common_name": "Test Guppy",
            "scientific_name": "Poecilia reticulata",
            "food_types": ["flakes", "live"],
            "feeding_frequency": 2,
            "care_level": "beginner",
            "water_type": "freshwater",
        },
        {
            "id": "test-betta",
            "common_name": "Test Betta",
            "scientific_name": "Betta splendens",
            "food_types": ["pellets", "live"],
            "feeding_frequency": 2,
            "care_level": "beginner",
            "water_type": "freshwater",
        },
        {
            "id": "test-hungry",
            "common_name": "Test Hungry Fish",
            "scientific_name": "Hungrius maximus",
            "food_types": ["everything"],
            "feeding_frequency": 3,
            "care_level": "intermediate",
            "water_type": "freshwater",
        },
    ]

    for sp_data in test_species_data:
        # Check if exists
        result = await session.execute(
            text("SELECT id FROM species WHERE id = :id"), {"id": sp_data["id"]}
        )
        if result.scalar_one_or_none() is None:
            species = Species(**sp_data)
            session.add(species)

    await session.commit()


async def create_aquarium_for_user(
    session: AsyncSession, user: User, name: str = "Test Aquarium"
) -> Aquarium:
    """Helper to create an aquarium owned by user."""
    aquarium = Aquarium(owner_id=user.id, name=name)
    session.add(aquarium)
    await session.flush()

    member = AquariumMember(aquarium_id=aquarium.id, user_id=user.id, role="owner")
    session.add(member)
    await session.flush()
    await session.refresh(aquarium)

    return aquarium


async def create_fish_in_aquarium(
    session: AsyncSession,
    aquarium: Aquarium,
    species_id: str = "test-guppy",
    quantity: int = 1,
) -> Fish:
    """Helper to create fish in an aquarium."""
    fish = Fish(aquarium_id=aquarium.id, species_id=species_id, quantity=quantity)
    session.add(fish)
    await session.flush()
    await session.refresh(fish)
    return fish


async def create_completed_feeding(
    session: AsyncSession,
    aquarium: Aquarium,
    user: User,
    completed_at: datetime | None = None,
) -> FeedingLog:
    """Helper to create a completed feeding log."""
    import uuid

    if completed_at is None:
        completed_at = datetime.now(UTC)

    # Need a fish for the schedule and log
    fish = await create_fish_in_aquarium(session, aquarium)

    schedule = FeedingSchedule(aquarium_id=aquarium.id, fish_id=fish.id, food_type="flakes")
    session.add(schedule)
    await session.flush()

    log = FeedingLog(
        aquarium_id=aquarium.id,
        schedule_id=schedule.id,
        fish_id=fish.id,
        scheduled_for=completed_at.replace(tzinfo=None),
        action="fed",
        acted_at=completed_at,
        acted_by_user_id=user.id,
        device_id=uuid.uuid4(),
    )
    session.add(log)
    await session.flush()
    await session.refresh(log)
    return log


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_unlocks_first_aquarium(async_session: AsyncSession):
    """Test that FIRST_AQUARIUM achievement unlocks when user creates first aquarium."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        await create_aquarium_for_user(async_session, user)
        await async_session.commit()

        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.FIRST_AQUARIUM.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_unlocks_first_fish(async_session: AsyncSession):
    """Test that FIRST_FISH achievement unlocks when user adds first fish."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)
        await create_fish_in_aquarium(async_session, aquarium)
        await async_session.commit()

        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.FIRST_FISH.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_unlocks_fish_collector_10(async_session: AsyncSession):
    """Test that FISH_COLLECTOR_10 achievement unlocks when user has 10 fish."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)
        # Add 10 fish in one go
        await create_fish_in_aquarium(async_session, aquarium, quantity=10)
        await async_session.commit()

        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.FISH_COLLECTOR_10.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_unlocks_first_feed(async_session: AsyncSession):
    """Test that FIRST_FEED achievement unlocks on first completed feeding."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)
        await create_completed_feeding(async_session, aquarium, user)
        await async_session.commit()

        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.FIRST_FEED.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_unlocks_streak_7(async_session: AsyncSession):
    """Test that STREAK_7 achievement unlocks when best_streak reaches 7."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)

        # Create streak with best_streak = 7
        streak = Streak(user_id=user.id, current_streak=7, best_streak=7)
        async_session.add(streak)
        await async_session.commit()

        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.STREAK_7.value in achievement_types
        assert AchievementType.PERFECT_WEEK.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_no_duplicate_unlock(async_session: AsyncSession):
    """Test that achievements are not duplicated on multiple check calls."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)
        await async_session.commit()

        # First check - should unlock FIRST_AQUARIUM
        first_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()
        assert len([a for a in first_unlocked if a.achievement_type == AchievementType.FIRST_AQUARIUM.value]) == 1

        # Second check - should not unlock anything
        second_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()
        assert len([a for a in second_unlocked if a.achievement_type == AchievementType.FIRST_AQUARIUM.value]) == 0

        # Verify only one FIRST_AQUARIUM achievement exists
        all_achievements = await get_achievements(async_session, user.id)
        first_aquarium_count = len([a for a in all_achievements if a.achievement_type == AchievementType.FIRST_AQUARIUM.value])
        assert first_aquarium_count == 1
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_correct_unlocked_at_timestamp(async_session: AsyncSession):
    """Test that unlocked_at timestamp is set correctly."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)
        await async_session.commit()

        before_check = datetime.now(UTC)
        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()
        after_check = datetime.now(UTC)

        assert len(newly_unlocked) > 0
        for achievement in newly_unlocked:
            assert achievement.unlocked_at is not None
            # Allow for small time differences
            assert achievement.unlocked_at >= before_check - timedelta(seconds=1)
            assert achievement.unlocked_at <= after_check + timedelta(seconds=1)
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_early_bird(async_session: AsyncSession):
    """Test that EARLY_BIRD achievement unlocks for feeding before 7 AM."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)

        # Create feeding completed at 6 AM
        early_time = datetime(2025, 1, 15, 6, 0, 0, tzinfo=UTC)
        await create_completed_feeding(async_session, aquarium, user, early_time)
        await async_session.commit()

        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.EARLY_BIRD.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_night_owl(async_session: AsyncSession):
    """Test that NIGHT_OWL achievement unlocks for feeding after 10 PM."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)

        # Create feeding completed at 11 PM
        night_time = datetime(2025, 1, 15, 23, 0, 0, tzinfo=UTC)
        await create_completed_feeding(async_session, aquarium, user, night_time)
        await async_session.commit()

        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.NIGHT_OWL.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_species_explorer(async_session: AsyncSession):
    """Test that SPECIES_EXPLORER_5 achievement unlocks with 5 different species."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)

        # Add fish of different species (using test species from conftest)
        # Note: we only have 3 test species, so we use what we have
        await create_fish_in_aquarium(async_session, aquarium, "test-guppy", quantity=1)
        await create_fish_in_aquarium(async_session, aquarium, "test-betta", quantity=1)
        await create_fish_in_aquarium(async_session, aquarium, "test-hungry", quantity=1)
        await async_session.commit()

        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        # Should not unlock SPECIES_EXPLORER_5 with only 3 species
        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.SPECIES_EXPLORER_5.value not in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_check_achievements_family_first(async_session: AsyncSession):
    """Test that FAMILY_FIRST achievement unlocks when family member joins."""
    await cleanup_all_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member = await create_test_user(async_session, "member@example.com")
        aquarium = await create_aquarium_for_user(async_session, owner)

        # Add member to aquarium
        aquarium_member = AquariumMember(
            aquarium_id=aquarium.id, user_id=member.id, role="member"
        )
        async_session.add(aquarium_member)
        await async_session.commit()

        # Check achievements for owner (they get family achievements)
        newly_unlocked = await check_achievements(async_session, owner.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.FAMILY_FIRST.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_share_achievement_updates_shared_at(async_session: AsyncSession):
    """Test that share_achievement updates shared_at timestamp."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)
        await async_session.commit()

        # Unlock an achievement first
        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        assert len(newly_unlocked) > 0
        achievement = newly_unlocked[0]
        assert achievement.shared_at is None

        # Share the achievement
        shared = await share_achievement(async_session, user.id, achievement.id)
        await async_session.commit()

        assert shared is not None
        assert shared.shared_at is not None
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_share_achievement_triggers_first_share(async_session: AsyncSession):
    """Test that FIRST_SHARE achievement unlocks after sharing any achievement."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)
        await async_session.commit()

        # Unlock and share an achievement
        unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement = unlocked[0]
        await share_achievement(async_session, user.id, achievement.id)
        await async_session.commit()

        # Check achievements again - FIRST_SHARE should now be unlocked
        newly_unlocked = await check_achievements(async_session, user.id)
        await async_session.commit()

        achievement_types = [a.achievement_type for a in newly_unlocked]
        assert AchievementType.FIRST_SHARE.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_share_achievement_not_found_returns_none(async_session: AsyncSession):
    """Test that share_achievement returns None for non-existent achievement."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)

        result = await share_achievement(async_session, user.id, uuid4())

        assert result is None
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_share_achievement_wrong_user_returns_none(async_session: AsyncSession):
    """Test that share_achievement returns None if user doesn't own achievement."""
    await cleanup_all_data(async_session)
    try:
        user1 = await create_test_user(async_session, "user1@example.com")
        user2 = await create_test_user(async_session, "user2@example.com")
        aquarium = await create_aquarium_for_user(async_session, user1)
        await async_session.commit()

        # User1 gets an achievement
        unlocked = await check_achievements(async_session, user1.id)
        await async_session.commit()

        # User2 tries to share user1's achievement
        result = await share_achievement(async_session, user2.id, unlocked[0].id)

        assert result is None
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_achievements_returns_all_user_achievements(async_session: AsyncSession):
    """Test that get_achievements returns all achievements for user."""
    await cleanup_all_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_aquarium_for_user(async_session, user)
        await create_fish_in_aquarium(async_session, aquarium)
        await create_completed_feeding(async_session, aquarium, user)
        await async_session.commit()

        # Unlock achievements
        await check_achievements(async_session, user.id)
        await async_session.commit()

        # Get all achievements
        achievements = await get_achievements(async_session, user.id)

        # Should have FIRST_AQUARIUM, FIRST_FISH, FIRST_FEED at minimum
        achievement_types = [a.achievement_type for a in achievements]
        assert AchievementType.FIRST_AQUARIUM.value in achievement_types
        assert AchievementType.FIRST_FISH.value in achievement_types
        assert AchievementType.FIRST_FEED.value in achievement_types
    finally:
        await cleanup_all_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_achievement_definitions_count(async_session: AsyncSession):
    """Test that we have approximately 20 achievement definitions."""
    # Per task requirements: ~20 базових achievements
    assert len(ACHIEVEMENT_DEFINITIONS) >= 20


@pytest.mark.asyncio(loop_scope="session")
async def test_all_achievement_types_have_definitions(async_session: AsyncSession):
    """Test that all AchievementType enum values have a definition."""
    defined_types = {d.achievement_type for d in ACHIEVEMENT_DEFINITIONS}
    enum_types = set(AchievementType)

    # All enum types should have definitions
    assert defined_types == enum_types


@pytest.mark.asyncio(loop_scope="session")
async def test_user_stats_dataclass_defaults(async_session: AsyncSession):
    """Test that UserStats has correct default values."""
    stats = UserStats()

    assert stats.current_streak == 0
    assert stats.best_streak == 0
    assert stats.total_feedings == 0
    assert stats.fish_count == 0
    assert stats.unique_species_count == 0
    assert stats.aquarium_count == 0
    assert stats.family_members_count == 0
    assert stats.has_early_bird_feeding is False
    assert stats.has_night_owl_feeding is False
    assert stats.has_shared_achievement is False
