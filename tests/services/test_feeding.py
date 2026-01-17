"""Integration tests for feeding service."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingEvent
from app.models.species import Species
from app.models.user import User
from app.schemas.feeding import ScheduleUpdate
from app.schemas.fish import FishCreate
from app.services.aquarium import AquariumAccessDeniedError
from app.services.feeding import (
    DEFAULT_TIMES,
    EventAlreadyCompletedError,
    EventNotFoundError,
    ScheduleNotFoundError,
    create_daily_events,
    generate_schedule,
    get_schedule,
    get_today_events,
    mark_as_fed,
    mark_as_missed,
    update_schedule,
)
from app.services.fish import add_fish


async def cleanup_feeding_data(session: AsyncSession) -> None:
    """Helper to cleanup feeding-related data."""
    await session.execute(text("DELETE FROM feeding_events"))
    await session.execute(text("DELETE FROM feeding_schedules"))
    await session.execute(text("DELETE FROM fish"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
    await session.execute(text("DELETE FROM species"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str | None = None,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def create_test_species(
    session: AsyncSession,
    species_id: str = "guppy",
    common_name: str = "Guppy",
    feeding_frequency: int = 2,
    food_types: list[str] | None = None,
) -> Species:
    """Helper to create a test species with configurable feeding frequency."""
    species = Species(
        id=species_id,
        common_name=common_name,
        scientific_name="Test scientific name",
        food_types=food_types or ["flakes"],
        feeding_frequency=feeding_frequency,
        care_level="beginner",
        water_type="freshwater",
    )
    session.add(species)
    await session.commit()
    await session.refresh(species)
    return species


async def create_test_aquarium(
    session: AsyncSession,
    owner: User,
    name: str = "Test Aquarium",
) -> Aquarium:
    """Helper to create a test aquarium with owner as member."""
    aquarium = Aquarium(
        owner_id=owner.id,
        name=name,
    )
    session.add(aquarium)
    await session.flush()

    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=owner.id,
        role="owner",
    )
    session.add(member)
    await session.commit()
    await session.refresh(aquarium)
    return aquarium


# generate_schedule tests


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_schedule_with_one_fish_twice_daily(async_session: AsyncSession):
    """Test generate_schedule with 1 fish (twice_daily) creates 2 scheduled_times."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(
            async_session, "guppy", "Guppy", feeding_frequency=2
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session,
            aquarium.id,
            user.id,
            FishCreate(species_id=species.id),
        )

        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        assert schedule is not None
        assert schedule.times_per_day == 2
        assert schedule.scheduled_times == ["08:00", "20:00"]
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_schedule_selects_max_frequency(async_session: AsyncSession):
    """Test generate_schedule with different frequency fish selects max."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species1 = await create_test_species(
            async_session, "guppy", "Guppy", feeding_frequency=1
        )
        species2 = await create_test_species(
            async_session, "betta", "Betta", feeding_frequency=3
        )
        species3 = await create_test_species(
            async_session, "goldfish", "Goldfish", feeding_frequency=2
        )
        aquarium = await create_test_aquarium(async_session, user)

        # Add fish with different feeding frequencies
        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species1.id)
        )
        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species2.id)
        )
        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species3.id)
        )

        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        # Should select max frequency (3)
        assert schedule.times_per_day == 3
        assert schedule.scheduled_times == ["08:00", "14:00", "20:00"]
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_schedule_without_fish_returns_default(
    async_session: AsyncSession,
):
    """Test generate_schedule without fish returns default schedule."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        # Default is 2x per day
        assert schedule.times_per_day == 2
        assert schedule.scheduled_times == ["08:00", "20:00"]
        assert schedule.food_type == "flakes"
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_schedule_aggregates_food_types(async_session: AsyncSession):
    """Test generate_schedule aggregates food_types from all species."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species1 = await create_test_species(
            async_session,
            "guppy",
            "Guppy",
            feeding_frequency=2,
            food_types=["flakes", "pellets"],
        )
        species2 = await create_test_species(
            async_session,
            "betta",
            "Betta",
            feeding_frequency=2,
            food_types=["bloodworms", "pellets"],
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species1.id)
        )
        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species2.id)
        )

        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        # Food types should be aggregated and sorted
        assert "bloodworms" in schedule.food_type
        assert "flakes" in schedule.food_type
        assert "pellets" in schedule.food_type
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_schedule_updates_existing_schedule(
    async_session: AsyncSession,
):
    """Test generate_schedule updates existing schedule instead of creating new."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species1 = await create_test_species(
            async_session, "guppy", "Guppy", feeding_frequency=1
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species1.id)
        )

        # Generate initial schedule
        schedule1 = await generate_schedule(async_session, aquarium.id, user.id)
        schedule_id = schedule1.id
        assert schedule1.times_per_day == 1

        # Add fish with higher frequency
        species2 = await create_test_species(
            async_session, "betta", "Betta", feeding_frequency=3
        )
        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species2.id)
        )

        # Regenerate schedule
        schedule2 = await generate_schedule(async_session, aquarium.id, user.id)

        # Should be same schedule ID, updated
        assert schedule2.id == schedule_id
        assert schedule2.times_per_day == 3
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_schedule_creates_today_events(async_session: AsyncSession):
    """Test generate_schedule creates feeding events for today."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(
            async_session, "guppy", "Guppy", feeding_frequency=2
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        await generate_schedule(async_session, aquarium.id, user.id)

        # Check events were created for today
        events = await get_today_events(async_session, aquarium.id, user.id)

        assert len(events) == 2
        assert all(e.status == "pending" for e in events)
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_schedule_raises_403_for_non_member(
    async_session: AsyncSession,
):
    """Test generate_schedule raises 403 for user without access."""
    await cleanup_feeding_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await generate_schedule(async_session, aquarium.id, other_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_feeding_data(async_session)


# scheduled_times generation tests


@pytest.mark.asyncio(loop_scope="session")
async def test_scheduled_times_for_once_daily(async_session: AsyncSession):
    """Test scheduled_times generated correctly for 1 time per day."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(
            async_session, "guppy", "Guppy", feeding_frequency=1
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        assert schedule.scheduled_times == DEFAULT_TIMES[1]
        assert schedule.scheduled_times == ["08:00"]
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scheduled_times_for_twice_daily(async_session: AsyncSession):
    """Test scheduled_times generated correctly for 2 times per day."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(
            async_session, "guppy", "Guppy", feeding_frequency=2
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        assert schedule.scheduled_times == DEFAULT_TIMES[2]
        assert schedule.scheduled_times == ["08:00", "20:00"]
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_scheduled_times_for_three_times_daily(async_session: AsyncSession):
    """Test scheduled_times generated correctly for 3 times per day."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(
            async_session, "guppy", "Guppy", feeding_frequency=3
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        assert schedule.scheduled_times == DEFAULT_TIMES[3]
        assert schedule.scheduled_times == ["08:00", "14:00", "20:00"]
    finally:
        await cleanup_feeding_data(async_session)


# get_schedule tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_schedule_returns_schedule(async_session: AsyncSession):
    """Test get_schedule returns existing schedule."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule
        created_schedule = await generate_schedule(async_session, aquarium.id, user.id)

        schedule = await get_schedule(async_session, aquarium.id, user.id)

        assert schedule is not None
        assert schedule.id == created_schedule.id
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_schedule_returns_none_if_not_exists(async_session: AsyncSession):
    """Test get_schedule returns None if no schedule exists."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = await get_schedule(async_session, aquarium.id, user.id)

        assert schedule is None
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_schedule_raises_403_for_non_member(async_session: AsyncSession):
    """Test get_schedule raises 403 for user without access."""
    await cleanup_feeding_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await get_schedule(async_session, aquarium.id, other_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_feeding_data(async_session)


# update_schedule tests


@pytest.mark.asyncio(loop_scope="session")
async def test_update_schedule_updates_times(async_session: AsyncSession):
    """Test update_schedule updates scheduled_times."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create initial schedule
        await generate_schedule(async_session, aquarium.id, user.id)

        # Update schedule
        update_data = ScheduleUpdate(
            times_per_day=3,
            scheduled_times=[time(9, 0), time(15, 0), time(21, 0)],
        )
        updated = await update_schedule(
            async_session, aquarium.id, user.id, update_data
        )

        assert updated.times_per_day == 3
        assert updated.scheduled_times == ["09:00", "15:00", "21:00"]
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_schedule_raises_404_if_not_exists(async_session: AsyncSession):
    """Test update_schedule raises 404 if no schedule exists."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        update_data = ScheduleUpdate(times_per_day=1, scheduled_times=[time(10, 0)])

        with pytest.raises(ScheduleNotFoundError) as exc_info:
            await update_schedule(async_session, aquarium.id, user.id, update_data)

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_update_schedule_regenerates_events(async_session: AsyncSession):
    """Test update_schedule regenerates today's events."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create initial schedule (2 events)
        await generate_schedule(async_session, aquarium.id, user.id)
        initial_events = await get_today_events(async_session, aquarium.id, user.id)
        assert len(initial_events) == 2

        # Update to 3 times per day
        update_data = ScheduleUpdate(
            times_per_day=3,
            scheduled_times=[time(9, 0), time(15, 0), time(21, 0)],
        )
        await update_schedule(async_session, aquarium.id, user.id, update_data)

        # Should have 3 events now
        updated_events = await get_today_events(async_session, aquarium.id, user.id)
        assert len(updated_events) == 3
    finally:
        await cleanup_feeding_data(async_session)


# get_today_events tests


@pytest.mark.asyncio(loop_scope="session")
async def test_get_today_events_returns_only_today(async_session: AsyncSession):
    """Test get_today_events returns only today's events."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule
        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        # Manually add event for yesterday
        yesterday = datetime.now(UTC) - timedelta(days=1)
        yesterday_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=yesterday,
            status="completed",
        )
        async_session.add(yesterday_event)
        await async_session.commit()

        events = await get_today_events(async_session, aquarium.id, user.id)

        # Should not include yesterday's event
        for event in events:
            assert event.scheduled_at.date() == datetime.now(UTC).date()
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_today_events_sorted_by_scheduled_at(async_session: AsyncSession):
    """Test get_today_events returns events sorted by scheduled_at."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        species = await create_test_species(
            async_session, "guppy", "Guppy", feeding_frequency=3
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        # Generate schedule with 3 times
        await generate_schedule(async_session, aquarium.id, user.id)

        events = await get_today_events(async_session, aquarium.id, user.id)

        assert len(events) == 3
        # Verify sorted order
        for i in range(len(events) - 1):
            assert events[i].scheduled_at <= events[i + 1].scheduled_at
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_today_events_raises_403_for_non_member(async_session: AsyncSession):
    """Test get_today_events raises 403 for user without access."""
    await cleanup_feeding_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await get_today_events(async_session, aquarium.id, other_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_feeding_data(async_session)


# mark_as_fed tests


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_as_fed_updates_status_and_completed_by(
    async_session: AsyncSession,
):
    """Test mark_as_fed updates status and completed_by."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule and events
        await generate_schedule(async_session, aquarium.id, user.id)
        events = await get_today_events(async_session, aquarium.id, user.id)
        event_id = events[0].id

        # Mark as fed
        updated_event = await mark_as_fed(async_session, event_id, user.id)

        assert updated_event.status == "completed"
        assert updated_event.completed_by == user.id
        assert updated_event.completed_at is not None
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_as_fed_raises_404_for_nonexistent(async_session: AsyncSession):
    """Test mark_as_fed raises 404 for non-existent event."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        random_id = uuid.uuid4()

        with pytest.raises(EventNotFoundError) as exc_info:
            await mark_as_fed(async_session, random_id, user.id)

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_as_fed_raises_error_for_already_completed(
    async_session: AsyncSession,
):
    """Test mark_as_fed raises error for already completed event."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule and events
        await generate_schedule(async_session, aquarium.id, user.id)
        events = await get_today_events(async_session, aquarium.id, user.id)
        event_id = events[0].id

        # Mark as fed first time
        await mark_as_fed(async_session, event_id, user.id)

        # Try to mark again
        with pytest.raises(EventAlreadyCompletedError) as exc_info:
            await mark_as_fed(async_session, event_id, user.id)

        assert exc_info.value.status_code == 400
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_as_fed_raises_403_for_non_member(async_session: AsyncSession):
    """Test mark_as_fed raises 403 for user without access."""
    await cleanup_feeding_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        # Create schedule and events
        await generate_schedule(async_session, aquarium.id, owner.id)
        events = await get_today_events(async_session, aquarium.id, owner.id)
        event_id = events[0].id

        with pytest.raises(AquariumAccessDeniedError) as exc_info:
            await mark_as_fed(async_session, event_id, other_user.id)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_as_fed_allows_member_access(async_session: AsyncSession):
    """Test mark_as_fed allows members to mark events."""
    await cleanup_feeding_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner)

        # Add member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Create schedule and events
        await generate_schedule(async_session, aquarium.id, owner.id)
        events = await get_today_events(async_session, aquarium.id, owner.id)
        event_id = events[0].id

        # Member should be able to mark as fed
        updated_event = await mark_as_fed(async_session, event_id, member_user.id)

        assert updated_event.status == "completed"
        assert updated_event.completed_by == member_user.id
    finally:
        await cleanup_feeding_data(async_session)


# mark_as_missed tests


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_as_missed_updates_status(async_session: AsyncSession):
    """Test mark_as_missed updates status to missed."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule and events
        await generate_schedule(async_session, aquarium.id, user.id)
        events = await get_today_events(async_session, aquarium.id, user.id)
        event_id = events[0].id

        # Mark as missed
        updated_event = await mark_as_missed(async_session, event_id)

        assert updated_event.status == "missed"
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_as_missed_raises_404_for_nonexistent(async_session: AsyncSession):
    """Test mark_as_missed raises 404 for non-existent event."""
    await cleanup_feeding_data(async_session)
    try:
        random_id = uuid.uuid4()

        with pytest.raises(EventNotFoundError) as exc_info:
            await mark_as_missed(async_session, random_id)

        assert exc_info.value.status_code == 404
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_as_missed_does_not_change_completed_event(
    async_session: AsyncSession,
):
    """Test mark_as_missed does not change already completed event."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule and events
        await generate_schedule(async_session, aquarium.id, user.id)
        events = await get_today_events(async_session, aquarium.id, user.id)
        event_id = events[0].id

        # First mark as fed
        await mark_as_fed(async_session, event_id, user.id)

        # Try to mark as missed
        event = await mark_as_missed(async_session, event_id)

        # Status should still be completed
        assert event.status == "completed"
    finally:
        await cleanup_feeding_data(async_session)


# create_daily_events tests


@pytest.mark.asyncio(loop_scope="session")
async def test_create_daily_events_creates_events_for_all_schedules(
    async_session: AsyncSession,
):
    """Test create_daily_events creates events for all active schedules."""
    await cleanup_feeding_data(async_session)
    try:
        user1 = await create_test_user(async_session, "user1@example.com")
        user2 = await create_test_user(async_session, "user2@example.com")
        aquarium1 = await create_test_aquarium(async_session, user1, "Aquarium 1")
        aquarium2 = await create_test_aquarium(async_session, user2, "Aquarium 2")

        # Create schedules for both aquariums
        await generate_schedule(async_session, aquarium1.id, user1.id)
        await generate_schedule(async_session, aquarium2.id, user2.id)

        # Clear today's events that were auto-created
        await async_session.execute(text("DELETE FROM feeding_events"))
        await async_session.commit()

        # Create events for today
        tomorrow = date.today() + timedelta(days=1)
        count = await create_daily_events(async_session, tomorrow)

        # Each schedule has 2 times per day (default)
        assert count == 4
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_daily_events_does_not_create_duplicates(
    async_session: AsyncSession,
):
    """Test create_daily_events doesn't create duplicate events."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule (creates today's events)
        await generate_schedule(async_session, aquarium.id, user.id)

        today = date.today()

        # Try to create events again for today
        count = await create_daily_events(async_session, today)

        # Should not create duplicates
        assert count == 0

        # Verify still only 2 events exist
        events = await get_today_events(async_session, aquarium.id, user.id)
        assert len(events) == 2
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_daily_events_for_future_date(async_session: AsyncSession):
    """Test create_daily_events can create events for future dates."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule
        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        # Create events for tomorrow
        tomorrow = date.today() + timedelta(days=1)
        count = await create_daily_events(async_session, tomorrow)

        assert count == 2  # Default schedule has 2 times per day

        # Verify events exist for tomorrow
        tomorrow_start = datetime.combine(tomorrow, time.min, tzinfo=UTC)
        tomorrow_end = datetime.combine(tomorrow, time.max, tzinfo=UTC)

        stmt = (
            select(FeedingEvent)
            .where(FeedingEvent.schedule_id == schedule.id)
            .where(FeedingEvent.scheduled_at >= tomorrow_start)
            .where(FeedingEvent.scheduled_at <= tomorrow_end)
        )
        result = await async_session.execute(stmt)
        events = list(result.scalars().all())

        assert len(events) == 2
    finally:
        await cleanup_feeding_data(async_session)


# Edge case tests


@pytest.mark.asyncio(loop_scope="session")
async def test_regenerate_events_preserves_completed_events(
    async_session: AsyncSession,
):
    """Test that regenerating events preserves completed/missed events."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule
        await generate_schedule(async_session, aquarium.id, user.id)
        events = await get_today_events(async_session, aquarium.id, user.id)

        # Mark first event as fed
        await mark_as_fed(async_session, events[0].id, user.id)
        completed_event_id = events[0].id

        # Update schedule (triggers regeneration)
        update_data = ScheduleUpdate(food_type="pellets")
        await update_schedule(async_session, aquarium.id, user.id, update_data)

        # Completed event should still exist
        stmt = select(FeedingEvent).where(FeedingEvent.id == completed_event_id)
        result = await async_session.execute(stmt)
        event = result.scalar_one_or_none()

        assert event is not None
        assert event.status == "completed"
    finally:
        await cleanup_feeding_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_schedule_clamps_frequency_to_valid_range(
    async_session: AsyncSession,
):
    """Test generate_schedule clamps frequency to 1-3 range."""
    await cleanup_feeding_data(async_session)
    try:
        user = await create_test_user(async_session)
        # Create species with frequency > 3
        species = await create_test_species(
            async_session, "extreme", "Extreme Fish", feeding_frequency=10
        )
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )

        schedule = await generate_schedule(async_session, aquarium.id, user.id)

        # Should be clamped to 3
        assert schedule.times_per_day == 3
        assert schedule.scheduled_times == ["08:00", "14:00", "20:00"]
    finally:
        await cleanup_feeding_data(async_session)
