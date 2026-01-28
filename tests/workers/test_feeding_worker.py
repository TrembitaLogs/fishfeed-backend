"""Tests for feeding worker background jobs."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingEvent, FeedingSchedule
from app.models.species import Species
from app.models.user import User
from app.schemas.fish import FishCreate
from app.services.feeding import generate_schedule
from app.services.fish import add_fish


async def cleanup_worker_data(session: AsyncSession) -> None:
    """Helper to cleanup worker-related data."""
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
    feeding_frequency: int = 2,
) -> Species:
    """Helper to create a test species."""
    species = Species(
        id=species_id,
        common_name="Test Fish",
        scientific_name="Testus fishicus",
        food_types=["flakes"],
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


# create_tomorrow_events_job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_create_tomorrow_events_job_creates_events(async_session: AsyncSession):
    """Test create_tomorrow_events_job creates events for all schedules."""
    await cleanup_worker_data(async_session)
    try:
        # Import here to avoid circular imports during patching
        from app.workers.feeding_worker import create_tomorrow_events_job

        user1 = await create_test_user(async_session, "user1@example.com")
        user2 = await create_test_user(async_session, "user2@example.com")
        species = await create_test_species(async_session, "test-species")
        aquarium1 = await create_test_aquarium(async_session, user1, "Aquarium 1")
        aquarium2 = await create_test_aquarium(async_session, user2, "Aquarium 2")

        # Add fish and generate schedules
        await add_fish(
            async_session, aquarium1.id, user1.id, FishCreate(species_id=species.id)
        )
        await add_fish(
            async_session, aquarium2.id, user2.id, FishCreate(species_id=species.id)
        )

        await generate_schedule(async_session, aquarium1.id, user1.id)
        await generate_schedule(async_session, aquarium2.id, user2.id)

        # Mock the session maker to use our test session
        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            # Create a context manager that returns our session
            mock_session_maker.return_value.__aenter__ = (
                lambda self: async_session.__aenter__()
            )
            mock_session_maker.return_value.__aexit__ = (
                lambda self, *args: async_session.__aexit__(*args)
            )

            # We need to use a custom implementation
            class MockSessionContext:
                async def __aenter__(self):
                    return async_session

                async def __aexit__(self, *args):
                    pass

            mock_session_maker.return_value = MockSessionContext()

            count = await create_tomorrow_events_job()

        # Each schedule has 2 times per day (default)
        assert count == 4

        # Verify events exist for tomorrow
        tomorrow = date.today() + timedelta(days=1)
        tomorrow_start = datetime.combine(tomorrow, time.min, tzinfo=UTC)
        tomorrow_end = datetime.combine(tomorrow, time.max, tzinfo=UTC)

        stmt = (
            select(FeedingEvent)
            .where(FeedingEvent.scheduled_at >= tomorrow_start)
            .where(FeedingEvent.scheduled_at <= tomorrow_end)
        )
        result = await async_session.execute(stmt)
        events = list(result.scalars().all())

        assert len(events) == 4
    finally:
        await cleanup_worker_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_tomorrow_events_job_no_duplicates(async_session: AsyncSession):
    """Test create_tomorrow_events_job doesn't create duplicates on re-run."""
    await cleanup_worker_data(async_session)
    try:
        from app.workers.feeding_worker import create_tomorrow_events_job

        user = await create_test_user(async_session)
        species = await create_test_species(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )
        await generate_schedule(async_session, aquarium.id, user.id)

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            # Run twice
            count1 = await create_tomorrow_events_job()
            count2 = await create_tomorrow_events_job()

        # First run creates events, second should not
        assert count1 == 2
        assert count2 == 0
    finally:
        await cleanup_worker_data(async_session)


# mark_overdue_as_missed_job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_overdue_as_missed_job_marks_old_events(async_session: AsyncSession):
    """Test mark_overdue_as_missed_job marks old pending events."""
    await cleanup_worker_data(async_session)
    try:
        from app.workers.feeding_worker import mark_overdue_as_missed_job

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        # Create schedule
        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=2,
            scheduled_times=["08:00", "20:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        # Create old pending event (3 hours ago, beyond threshold)
        old_time = datetime.now(UTC) - timedelta(hours=3)
        old_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=old_time,
            status="pending",
        )
        async_session.add(old_event)

        # Create recent pending event (30 minutes ago, within threshold)
        recent_time = datetime.now(UTC) - timedelta(minutes=30)
        recent_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=recent_time,
            status="pending",
        )
        async_session.add(recent_event)
        await async_session.commit()

        old_event_id = old_event.id
        recent_event_id = recent_event.id

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            count = await mark_overdue_as_missed_job()

        assert count == 1

        # Verify old event is missed
        stmt = select(FeedingEvent).where(FeedingEvent.id == old_event_id)
        result = await async_session.execute(stmt)
        old = result.scalar_one()
        assert old.status == "missed"

        # Verify recent event is still pending
        stmt = select(FeedingEvent).where(FeedingEvent.id == recent_event_id)
        result = await async_session.execute(stmt)
        recent = result.scalar_one()
        assert recent.status == "pending"
    finally:
        await cleanup_worker_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mark_overdue_as_missed_job_ignores_completed(
    async_session: AsyncSession,
):
    """Test mark_overdue_as_missed_job ignores already completed events."""
    await cleanup_worker_data(async_session)
    try:
        from app.workers.feeding_worker import mark_overdue_as_missed_job

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        # Create old but completed event
        old_time = datetime.now(UTC) - timedelta(hours=5)
        completed_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=old_time,
            status="completed",
            completed_at=old_time + timedelta(minutes=5),
            completed_by=user.id,
        )
        async_session.add(completed_event)
        await async_session.commit()

        event_id = completed_event.id

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            count = await mark_overdue_as_missed_job()

        # No events should be marked as missed
        assert count == 0

        # Verify event is still completed
        stmt = select(FeedingEvent).where(FeedingEvent.id == event_id)
        result = await async_session.execute(stmt)
        event = result.scalar_one()
        assert event.status == "completed"
    finally:
        await cleanup_worker_data(async_session)


# cleanup_old_events_job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_old_events_job_deletes_old_events(async_session: AsyncSession):
    """Test cleanup_old_events_job deletes events older than retention period."""
    await cleanup_worker_data(async_session)
    try:
        from app.workers.feeding_worker import cleanup_old_events_job

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        # Create old event (100 days ago, beyond 90 day retention)
        old_time = datetime.now(UTC) - timedelta(days=100)
        old_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=old_time,
            status="completed",
        )
        async_session.add(old_event)

        # Create recent event (within retention)
        recent_time = datetime.now(UTC) - timedelta(days=30)
        recent_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=recent_time,
            status="completed",
        )
        async_session.add(recent_event)
        await async_session.commit()

        recent_event_id = recent_event.id

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            count = await cleanup_old_events_job()

        assert count == 1

        # Verify old event is deleted
        stmt = select(FeedingEvent).where(FeedingEvent.aquarium_id == aquarium.id)
        result = await async_session.execute(stmt)
        events = list(result.scalars().all())

        assert len(events) == 1
        assert events[0].id == recent_event_id
    finally:
        await cleanup_worker_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_old_events_job_no_events_to_delete(async_session: AsyncSession):
    """Test cleanup_old_events_job handles case with no old events."""
    await cleanup_worker_data(async_session)
    try:
        from app.workers.feeding_worker import cleanup_old_events_job

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=1,
            scheduled_times=["08:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        # Create only recent events
        recent_time = datetime.now(UTC) - timedelta(days=7)
        recent_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=recent_time,
            status="completed",
        )
        async_session.add(recent_event)
        await async_session.commit()

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            count = await cleanup_old_events_job()

        assert count == 0
    finally:
        await cleanup_worker_data(async_session)


# run_once tests


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_runs_all_jobs(async_session: AsyncSession):
    """Test run_once executes all jobs when no specific job is specified."""
    await cleanup_worker_data(async_session)
    try:
        from app.workers.feeding_worker import run_once

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)
        species = await create_test_species(async_session)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )
        await generate_schedule(async_session, aquarium.id, user.id)

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            # Should run without errors
            await run_once()
    finally:
        await cleanup_worker_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_runs_specific_job(async_session: AsyncSession):
    """Test run_once executes only specified job."""
    await cleanup_worker_data(async_session)
    try:
        from app.workers.feeding_worker import run_once

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)
        species = await create_test_species(async_session)

        await add_fish(
            async_session, aquarium.id, user.id, FishCreate(species_id=species.id)
        )
        await generate_schedule(async_session, aquarium.id, user.id)

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            # Should run only create_events job
            await run_once(job_name="create_events")
    finally:
        await cleanup_worker_data(async_session)


# Scheduler startup tests


@pytest.mark.asyncio(loop_scope="session")
async def test_start_scheduler_calls_start_in_background():
    """Test that start_scheduler() calls start_in_background() to process jobs.

    This is a regression test for a bug where scheduler was initialized
    but never started processing jobs because start_in_background() was missing.
    """
    from unittest.mock import AsyncMock, MagicMock

    from app.workers import feeding_worker

    # Save original state
    original_scheduler = feeding_worker._scheduler
    original_shutdown_event = feeding_worker._shutdown_event

    # Reset global state for test
    feeding_worker._scheduler = None
    feeding_worker._shutdown_event = None

    try:
        # Create mock scheduler
        mock_scheduler = MagicMock()
        mock_scheduler.__aenter__ = AsyncMock(return_value=mock_scheduler)
        mock_scheduler.__aexit__ = AsyncMock(return_value=None)
        mock_scheduler.add_schedule = AsyncMock()
        mock_scheduler.start_in_background = AsyncMock()

        with (
            patch.object(
                feeding_worker, "AsyncScheduler", return_value=mock_scheduler
            ),
            patch.object(feeding_worker, "SQLAlchemyDataStore"),
            patch.object(feeding_worker, "AsyncpgEventBroker"),
        ):
            await feeding_worker.start_scheduler()

            # Verify start_in_background was called - this is critical!
            mock_scheduler.start_in_background.assert_called_once()
    finally:
        # Restore original state
        feeding_worker._scheduler = original_scheduler
        feeding_worker._shutdown_event = original_shutdown_event


# Streak break detection tests


@pytest.mark.asyncio(loop_scope="session")
async def test_streak_break_detection_all_missed(async_session: AsyncSession):
    """Test streak break is detected when all daily events are missed."""
    await cleanup_worker_data(async_session)
    try:
        from app.workers.feeding_worker import mark_overdue_as_missed_job

        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user)

        schedule = FeedingSchedule(
            aquarium_id=aquarium.id,
            times_per_day=2,
            scheduled_times=["08:00", "20:00"],
            food_type="flakes",
        )
        async_session.add(schedule)
        await async_session.commit()
        await async_session.refresh(schedule)

        # Create all events for yesterday as pending (will be marked missed)
        yesterday = datetime.now(UTC).date() - timedelta(days=1)

        for hour in [8, 20]:
            event = FeedingEvent(
                aquarium_id=aquarium.id,
                schedule_id=schedule.id,
                scheduled_at=datetime.combine(
                    yesterday, time(hour, 0), tzinfo=UTC
                ),
                status="pending",
            )
            async_session.add(event)
        await async_session.commit()

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            # Capture log output to verify streak break detection
            with patch("app.workers.feeding_worker.logger") as mock_logger:
                count = await mark_overdue_as_missed_job()

                # Verify streak break warning was logged
                mock_logger.warning.assert_called()
                call_args = str(mock_logger.warning.call_args)
                assert "Streak break detected" in call_args

        assert count == 2
    finally:
        await cleanup_worker_data(async_session)
