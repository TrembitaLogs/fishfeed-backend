"""Tests for admin dashboard statistics service."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIScan
from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak
from app.models.user import User
from app.services.admin import get_dashboard_stats


async def cleanup(session: AsyncSession) -> None:
    """Truncate all relevant tables in dependency order."""
    await session.execute(text("TRUNCATE TABLE feeding_logs CASCADE"))
    await session.execute(text("TRUNCATE TABLE feeding_schedules CASCADE"))
    await session.execute(text("TRUNCATE TABLE ai_scans CASCADE"))
    await session.execute(text("TRUNCATE TABLE achievements CASCADE"))
    await session.execute(text("TRUNCATE TABLE streaks CASCADE"))
    await session.execute(text("TRUNCATE TABLE aquarium_members CASCADE"))
    await session.execute(text("TRUNCATE TABLE fish CASCADE"))
    await session.execute(text("TRUNCATE TABLE aquariums CASCADE"))
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.execute(
        text("DELETE FROM species WHERE id = 'admin-test-species'")
    )
    await session.commit()


async def _create_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    subscription_status: str = "free",
    created_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> User:
    """Helper to create a test user with optional overrides."""
    user = User(
        email=email or f"{uuid4().hex[:8]}@test.com",
        password_hash="hashed",
        subscription_status=subscription_status,
    )
    if created_at is not None:
        user.created_at = created_at
    if deleted_at is not None:
        user.deleted_at = deleted_at
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _create_aquarium(
    session: AsyncSession,
    owner: User,
    *,
    name: str = "Test Aquarium",
) -> Aquarium:
    """Helper to create a test aquarium."""
    aquarium = Aquarium(owner_id=owner.id, name=name)
    session.add(aquarium)
    await session.flush()
    await session.refresh(aquarium)
    return aquarium


async def _ensure_species(session: AsyncSession, species_id: str = "admin-test-species") -> str:
    """Ensure a species record exists for FK constraints. Returns species_id."""
    from sqlalchemy import select as sa_select

    from app.models.species import Species

    result = await session.execute(sa_select(Species).where(Species.id == species_id))
    if result.scalar_one_or_none() is None:
        species = Species(
            id=species_id,
            common_name="Admin Test Species",
            food_types=["flakes"],
            feeding_frequency=2,
            care_level="beginner",
            water_type="freshwater",
        )
        session.add(species)
        await session.flush()
    return species_id


async def _create_fish(
    session: AsyncSession,
    aquarium: Aquarium,
) -> Fish:
    """Helper to create a test fish with its own species."""
    species_id = await _ensure_species(session)
    fish = Fish(
        aquarium_id=aquarium.id,
        species_id=species_id,
        custom_name="Test Fish",
        quantity=1,
        added_via="manual",
    )
    session.add(fish)
    await session.flush()
    await session.refresh(fish)
    return fish


@pytest.mark.asyncio(loop_scope="session")
async def test_get_dashboard_stats_empty_db(async_session: AsyncSession):
    """All stats should be zero when the database is empty."""
    await cleanup(async_session)
    try:
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        result = await get_dashboard_stats(async_session, now=now)

        # Users
        assert result.users.total == 0
        assert result.users.active_last_7d == 0
        assert result.users.premium == 0
        assert result.users.new_today == 0

        # Aquariums
        assert result.aquariums.total == 0
        assert result.aquariums.with_family_members == 0

        # Feeding
        assert result.feeding.logs_today == 0
        assert result.feeding.schedules_active == 0

        # AI
        assert result.ai_scans.total == 0
        assert result.ai_scans.today == 0

        # Gamification
        assert result.gamification.avg_streak == 0.0
        assert result.gamification.max_streak == 0
        assert result.gamification.achievements_unlocked_today == 0
    finally:
        await cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_dashboard_stats_with_data(async_session: AsyncSession):
    """Stats should correctly reflect seeded data across all categories."""
    await cleanup(async_session)
    try:
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = now - timedelta(days=1)
        three_days_ago = now - timedelta(days=3)
        ten_days_ago = now - timedelta(days=10)

        # --- Users ---
        user1 = await _create_user(
            async_session, email="active@test.com", created_at=today_start
        )
        user2 = await _create_user(
            async_session,
            email="premium@test.com",
            subscription_status="premium",
            created_at=ten_days_ago,
        )
        user3 = await _create_user(
            async_session, email="old@test.com", created_at=ten_days_ago
        )
        # Soft-deleted user — should NOT count
        await _create_user(
            async_session,
            email="deleted@test.com",
            deleted_at=yesterday,
            created_at=ten_days_ago,
        )

        # --- Aquariums ---
        aquarium1 = await _create_aquarium(async_session, user1, name="Aqua 1")
        aquarium2 = await _create_aquarium(async_session, user2, name="Aqua 2")
        # Extra aquarium owned by user3 with no family members
        await _create_aquarium(async_session, user3, name="Aqua 3")

        # Family member on aquarium1
        member = AquariumMember(aquarium_id=aquarium1.id, user_id=user2.id, role="member")
        async_session.add(member)

        # Family member on aquarium2
        member2 = AquariumMember(aquarium_id=aquarium2.id, user_id=user1.id, role="member")
        async_session.add(member2)
        await async_session.flush()

        # --- Fish & Feeding Schedules ---
        fish1 = await _create_fish(async_session, aquarium1)
        fish2 = await _create_fish(async_session, aquarium2)

        # Active schedule
        schedule1 = FeedingSchedule(
            aquarium_id=aquarium1.id,
            fish_id=fish1.id,
            food_type="flakes",
            active=True,
        )
        # Inactive schedule
        schedule2 = FeedingSchedule(
            aquarium_id=aquarium2.id,
            fish_id=fish2.id,
            food_type="pellets",
            active=False,
        )
        async_session.add_all([schedule1, schedule2])
        await async_session.flush()
        await async_session.refresh(schedule1)
        await async_session.refresh(schedule2)

        # --- Feeding Logs ---
        device_id = uuid4()
        # Log today by user1
        log_today = FeedingLog(
            schedule_id=schedule1.id,
            fish_id=fish1.id,
            aquarium_id=aquarium1.id,
            scheduled_for=now.replace(tzinfo=None),
            action="fed",
            acted_at=now,
            acted_by_user_id=user1.id,
            device_id=device_id,
        )
        # Log today by user2
        log_today2 = FeedingLog(
            schedule_id=schedule2.id,
            fish_id=fish2.id,
            aquarium_id=aquarium2.id,
            scheduled_for=(now - timedelta(hours=2)).replace(tzinfo=None),
            action="fed",
            acted_at=now - timedelta(hours=1),
            acted_by_user_id=user2.id,
            device_id=device_id,
        )
        # Log 3 days ago by user1 (still within 7 days)
        log_3d_ago = FeedingLog(
            schedule_id=schedule1.id,
            fish_id=fish1.id,
            aquarium_id=aquarium1.id,
            scheduled_for=three_days_ago.replace(tzinfo=None),
            action="fed",
            acted_at=three_days_ago,
            acted_by_user_id=user1.id,
            device_id=device_id,
        )
        # Log 10 days ago by user3 (outside 7-day window)
        log_10d_ago = FeedingLog(
            schedule_id=schedule1.id,
            fish_id=fish1.id,
            aquarium_id=aquarium1.id,
            scheduled_for=ten_days_ago.replace(tzinfo=None),
            action="fed",
            acted_at=ten_days_ago,
            acted_by_user_id=user3.id,
            device_id=device_id,
        )
        async_session.add_all([log_today, log_today2, log_3d_ago, log_10d_ago])
        await async_session.flush()

        # --- AI Scans ---
        scan_today = AIScan(user_id=user1.id, created_at=now)
        scan_old = AIScan(user_id=user2.id, created_at=ten_days_ago)
        async_session.add_all([scan_today, scan_old])
        await async_session.flush()

        # --- Gamification ---
        streak1 = Streak(user_id=user1.id, current_streak=10, best_streak=15)
        streak2 = Streak(user_id=user2.id, current_streak=4, best_streak=20)
        async_session.add_all([streak1, streak2])
        await async_session.flush()

        achievement_today = Achievement(
            user_id=user1.id, achievement_type="first_feed", unlocked_at=now
        )
        achievement_old = Achievement(
            user_id=user2.id, achievement_type="streak_7", unlocked_at=ten_days_ago
        )
        async_session.add_all([achievement_today, achievement_old])
        await async_session.flush()

        # --- Execute ---
        result = await get_dashboard_stats(async_session, now=now)

        # Users: 3 non-deleted
        assert result.users.total == 3
        # Active last 7d: user1 and user2 (user3's log is 10 days ago)
        assert result.users.active_last_7d == 2
        # Premium: user2
        assert result.users.premium == 1
        # New today: user1
        assert result.users.new_today == 1

        # Aquariums: 3 total (none deleted), 2 with family members
        assert result.aquariums.total == 3
        assert result.aquariums.with_family_members == 2

        # Feeding: 2 logs today, 1 active schedule
        assert result.feeding.logs_today == 2
        assert result.feeding.schedules_active == 1

        # AI scans: 2 total, 1 today
        assert result.ai_scans.total == 2
        assert result.ai_scans.today == 1

        # Gamification: avg streak (10+4)/2=7.0, max streak 10, 1 achievement today
        assert result.gamification.avg_streak == 7.0
        assert result.gamification.max_streak == 10
        assert result.gamification.achievements_unlocked_today == 1
    finally:
        await cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_dashboard_stats_soft_deleted_users_excluded(async_session: AsyncSession):
    """Soft-deleted users and their premium status should not be counted."""
    await cleanup(async_session)
    try:
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        yesterday = now - timedelta(days=1)

        # One active premium user
        await _create_user(
            async_session,
            email="active_premium@test.com",
            subscription_status="premium",
        )
        # One soft-deleted premium user
        await _create_user(
            async_session,
            email="deleted_premium@test.com",
            subscription_status="premium",
            deleted_at=yesterday,
        )

        result = await get_dashboard_stats(async_session, now=now)

        assert result.users.total == 1
        assert result.users.premium == 1
    finally:
        await cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_dashboard_stats_aquarium_without_members(async_session: AsyncSession):
    """Aquariums without family members should not count as with_family_members."""
    await cleanup(async_session)
    try:
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        user = await _create_user(async_session)

        # Aquarium with no AquariumMember records
        await _create_aquarium(async_session, user, name="Solo Aquarium")

        result = await get_dashboard_stats(async_session, now=now)

        assert result.aquariums.total == 1
        assert result.aquariums.with_family_members == 0
    finally:
        await cleanup(async_session)
