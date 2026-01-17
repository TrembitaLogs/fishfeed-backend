import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Aquarium, FeedingEvent, FeedingSchedule, User


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_creation(async_session: AsyncSession):
    """Test FeedingSchedule creation with UUID primary key."""
    user = User(email="schedule_owner@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Schedule Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        times_per_day=3,
        food_type="pellets",
        portion_hint="Small pinch",
    )
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    assert isinstance(schedule.id, uuid.UUID)
    assert schedule.aquarium_id == aquarium.id
    assert schedule.times_per_day == 3
    assert schedule.food_type == "pellets"
    assert schedule.portion_hint == "Small pinch"


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_has_timestamp_mixin(async_session: AsyncSession):
    """Test that FeedingSchedule has TimestampMixin fields."""
    user = User(email="schedule_ts@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="TS Schedule Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    schedule = FeedingSchedule(aquarium_id=aquarium.id)
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    assert schedule.created_at is not None
    assert schedule.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_scheduled_times_jsonb(async_session: AsyncSession):
    """Test that FeedingSchedule scheduled_times accepts JSONB data."""
    user = User(email="schedule_jsonb@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="JSONB Schedule Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    scheduled_times = ["08:00", "14:00", "20:00"]
    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        times_per_day=3,
        scheduled_times=scheduled_times,
    )
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    assert schedule.scheduled_times == scheduled_times


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_default_values(async_session: AsyncSession):
    """Test FeedingSchedule default values."""
    user = User(email="schedule_defaults@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Defaults Schedule Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    schedule = FeedingSchedule(aquarium_id=aquarium.id)
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    assert schedule.times_per_day == 2
    assert schedule.scheduled_times == []
    assert schedule.food_type == "flakes"
    assert schedule.portion_hint is None


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_creation(async_session: AsyncSession):
    """Test FeedingEvent creation with UUID primary key."""
    user = User(email="event_owner@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Event Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    scheduled_at = datetime.now(UTC)
    event = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=scheduled_at,
        status="pending",
    )
    async_session.add(event)
    await async_session.commit()
    await async_session.refresh(event)

    assert isinstance(event.id, uuid.UUID)
    assert event.aquarium_id == aquarium.id
    assert event.scheduled_at == scheduled_at
    assert event.status == "pending"


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_has_timestamp_mixin(async_session: AsyncSession):
    """Test that FeedingEvent has TimestampMixin fields."""
    user = User(email="event_ts@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="TS Event Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    event = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=datetime.now(UTC),
    )
    async_session.add(event)
    await async_session.commit()
    await async_session.refresh(event)

    assert event.created_at is not None
    assert event.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_completion(async_session: AsyncSession):
    """Test FeedingEvent can be marked as completed."""
    user = User(email="event_complete@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Complete Event Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    scheduled_at = datetime.now(UTC)
    completed_at = datetime.now(UTC)

    event = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=scheduled_at,
        status="completed",
        completed_at=completed_at,
        completed_by=user.id,
    )
    async_session.add(event)
    await async_session.commit()
    await async_session.refresh(event)

    assert event.status == "completed"
    assert event.completed_at == completed_at
    assert event.completed_by == user.id


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_completed_by_user_relationship(async_session: AsyncSession):
    """Test FeedingEvent to User relationship via completed_by."""
    user = User(email="event_user_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="User Rel Event Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    event = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=datetime.now(UTC),
        status="completed",
        completed_by=user.id,
    )
    async_session.add(event)
    await async_session.commit()

    result = await async_session.execute(
        select(FeedingEvent).where(FeedingEvent.id == event.id)
    )
    loaded_event = result.scalar_one()
    await async_session.refresh(loaded_event, ["completed_by_user"])

    assert loaded_event.completed_by_user.id == user.id
    assert loaded_event.completed_by_user.email == "event_user_rel@example.com"


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_schedule_relationship(async_session: AsyncSession):
    """Test FeedingEvent to FeedingSchedule relationship."""
    user = User(email="event_schedule_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Schedule Rel Event Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    schedule = FeedingSchedule(aquarium_id=aquarium.id)
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    event = FeedingEvent(
        aquarium_id=aquarium.id,
        schedule_id=schedule.id,
        scheduled_at=datetime.now(UTC),
    )
    async_session.add(event)
    await async_session.commit()

    result = await async_session.execute(
        select(FeedingEvent).where(FeedingEvent.id == event.id)
    )
    loaded_event = result.scalar_one()
    await async_session.refresh(loaded_event, ["schedule"])

    assert loaded_event.schedule.id == schedule.id


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_events_relationship(async_session: AsyncSession):
    """Test FeedingSchedule to FeedingEvents relationship."""
    user = User(email="schedule_events_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Events Rel Schedule Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    schedule = FeedingSchedule(aquarium_id=aquarium.id)
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    event1 = FeedingEvent(
        aquarium_id=aquarium.id,
        schedule_id=schedule.id,
        scheduled_at=datetime.now(UTC),
    )
    event2 = FeedingEvent(
        aquarium_id=aquarium.id,
        schedule_id=schedule.id,
        scheduled_at=datetime.now(UTC),
    )
    async_session.add_all([event1, event2])
    await async_session.commit()

    result = await async_session.execute(
        select(FeedingSchedule).where(FeedingSchedule.id == schedule.id)
    )
    loaded_schedule = result.scalar_one()
    await async_session.refresh(loaded_schedule, ["feeding_events"])

    assert len(loaded_schedule.feeding_events) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_feeding_schedules_relationship(async_session: AsyncSession):
    """Test Aquarium to FeedingSchedule relationship."""
    user = User(email="aquarium_schedules_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Schedules Rel Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    schedule1 = FeedingSchedule(aquarium_id=aquarium.id, food_type="flakes")
    schedule2 = FeedingSchedule(aquarium_id=aquarium.id, food_type="pellets")
    async_session.add_all([schedule1, schedule2])
    await async_session.commit()

    result = await async_session.execute(
        select(Aquarium).where(Aquarium.id == aquarium.id)
    )
    loaded_aquarium = result.scalar_one()
    await async_session.refresh(loaded_aquarium, ["feeding_schedules"])

    assert len(loaded_aquarium.feeding_schedules) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_aquarium_feeding_events_relationship(async_session: AsyncSession):
    """Test Aquarium to FeedingEvent relationship."""
    user = User(email="aquarium_events_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Events Rel Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    event1 = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=datetime.now(UTC),
    )
    event2 = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=datetime.now(UTC),
    )
    async_session.add_all([event1, event2])
    await async_session.commit()

    result = await async_session.execute(
        select(Aquarium).where(Aquarium.id == aquarium.id)
    )
    loaded_aquarium = result.scalar_one()
    await async_session.refresh(loaded_aquarium, ["feeding_events"])

    assert len(loaded_aquarium.feeding_events) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_cascade_delete_from_aquarium(
    async_session: AsyncSession,
):
    """Test that FeedingSchedule is deleted when Aquarium is deleted."""
    user = User(email="schedule_cascade@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Cascade Schedule Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    schedule = FeedingSchedule(aquarium_id=aquarium.id)
    async_session.add(schedule)
    await async_session.commit()

    schedule_id = schedule.id
    await async_session.delete(aquarium)
    await async_session.commit()

    result = await async_session.execute(
        select(FeedingSchedule).where(FeedingSchedule.id == schedule_id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_cascade_delete_from_aquarium(async_session: AsyncSession):
    """Test that FeedingEvent is deleted when Aquarium is deleted."""
    user = User(email="event_cascade_aq@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Cascade Event Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    event = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=datetime.now(UTC),
    )
    async_session.add(event)
    await async_session.commit()

    event_id = event.id
    await async_session.delete(aquarium)
    await async_session.commit()

    result = await async_session.execute(
        select(FeedingEvent).where(FeedingEvent.id == event_id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_cascade_delete_from_schedule(async_session: AsyncSession):
    """Test that FeedingEvent is deleted when FeedingSchedule is deleted."""
    user = User(email="event_cascade_sched@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Cascade Sched Event Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    schedule = FeedingSchedule(aquarium_id=aquarium.id)
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    event = FeedingEvent(
        aquarium_id=aquarium.id,
        schedule_id=schedule.id,
        scheduled_at=datetime.now(UTC),
    )
    async_session.add(event)
    await async_session.commit()

    event_id = event.id
    await async_session.delete(schedule)
    await async_session.commit()

    result = await async_session.execute(
        select(FeedingEvent).where(FeedingEvent.id == event_id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_default_status(async_session: AsyncSession):
    """Test FeedingEvent default status is pending."""
    user = User(email="event_default_status@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Default Status Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    event = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=datetime.now(UTC),
    )
    async_session.add(event)
    await async_session.commit()
    await async_session.refresh(event)

    assert event.status == "pending"


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_client_created_at(async_session: AsyncSession):
    """Test FeedingEvent client_created_at for offline sync."""
    user = User(email="event_client_time@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Client Time Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    client_time = datetime.now(UTC)
    event = FeedingEvent(
        aquarium_id=aquarium.id,
        scheduled_at=datetime.now(UTC),
        client_created_at=client_time,
    )
    async_session.add(event)
    await async_session.commit()
    await async_session.refresh(event)

    assert event.client_created_at == client_time
