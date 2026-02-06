"""Tests for FeedingSchedule and FeedingLog models."""

import uuid
from datetime import date, datetime
from datetime import time as dt_time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Aquarium, FeedingLog, FeedingSchedule, Fish, Species, User


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_creation(async_session: AsyncSession):
    """Test FeedingSchedule creation with new per-fish schema."""
    user = User(email="schedule_owner@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Schedule Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(
        id="test-sched-sp",
        common_name="Test",
        scientific_name="Testus",
        food_types=["flakes"],
        feeding_frequency=2,
        care_level="beginner",
        water_type="freshwater",
    )
    async_session.add(species)
    await async_session.commit()

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
        food_type="pellets",
        portion_hint="Small pinch",
        active=True,
        created_by_user_id=user.id,
    )
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    assert isinstance(schedule.id, uuid.UUID)
    assert schedule.aquarium_id == aquarium.id
    assert schedule.fish_id == fish.id
    assert schedule.time == dt_time(9, 0)
    assert schedule.interval_days == 1
    assert schedule.food_type == "pellets"
    assert schedule.portion_hint == "Small pinch"
    assert schedule.active is True
    assert schedule.created_by_user_id == user.id


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

    species = Species(
        id="test-ts-sp",
        common_name="TSFish",
        scientific_name="Testus",
        food_types=["flakes"],
        feeding_frequency=1,
        care_level="beginner",
        water_type="freshwater",
    )
    async_session.add(species)
    await async_session.commit()

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
    )
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    assert schedule.created_at is not None
    assert schedule.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_log_creation(async_session: AsyncSession):
    """Test FeedingLog creation with UNIQUE constraint fields."""
    user = User(email="log_owner@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Log Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(
        id="test-log-sp",
        common_name="LogFish",
        scientific_name="Testus",
        food_types=["flakes"],
        feeding_frequency=1,
        care_level="beginner",
        water_type="freshwater",
    )
    async_session.add(species)
    await async_session.commit()

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
    )
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    scheduled_for = datetime(2025, 6, 15, 9, 0)
    log = FeedingLog(
        schedule_id=schedule.id,
        fish_id=fish.id,
        aquarium_id=aquarium.id,
        scheduled_for=scheduled_for,
        action="fed",
        acted_by_user_id=user.id,
        device_id=uuid.uuid4(),
    )
    async_session.add(log)
    await async_session.commit()
    await async_session.refresh(log)

    assert isinstance(log.id, uuid.UUID)
    assert log.schedule_id == schedule.id
    assert log.action == "fed"
    assert log.acted_by_user_id == user.id
    assert log.created_at is not None
    assert log.acted_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_log_user_relationship(async_session: AsyncSession):
    """Test FeedingLog to User relationship via acted_by_user."""
    user = User(email="log_user_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="User Rel Log Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(
        id="test-rel-sp",
        common_name="RelFish",
        scientific_name="Testus",
        food_types=["flakes"],
        feeding_frequency=1,
        care_level="beginner",
        water_type="freshwater",
    )
    async_session.add(species)
    await async_session.commit()

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
    )
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    log = FeedingLog(
        schedule_id=schedule.id,
        fish_id=fish.id,
        aquarium_id=aquarium.id,
        scheduled_for=datetime(2025, 6, 15, 9, 0),
        action="fed",
        acted_by_user_id=user.id,
        device_id=uuid.uuid4(),
    )
    async_session.add(log)
    await async_session.commit()

    result = await async_session.execute(select(FeedingLog).where(FeedingLog.id == log.id))
    loaded_log = result.scalar_one()
    await async_session.refresh(loaded_log, ["acted_by_user"])

    assert loaded_log.acted_by_user.id == user.id
    assert loaded_log.acted_by_user.email == "log_user_rel@example.com"


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_logs_relationship(async_session: AsyncSession):
    """Test FeedingSchedule to FeedingLogs cascade relationship."""
    user = User(email="schedule_logs_rel@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Logs Rel Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(
        id="test-logs-rel-sp",
        common_name="LogsRelFish",
        scientific_name="Testus",
        food_types=["flakes"],
        feeding_frequency=1,
        care_level="beginner",
        water_type="freshwater",
    )
    async_session.add(species)
    await async_session.commit()

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
    )
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    log1 = FeedingLog(
        schedule_id=schedule.id,
        fish_id=fish.id,
        aquarium_id=aquarium.id,
        scheduled_for=datetime(2025, 6, 15, 9, 0),
        action="fed",
        acted_by_user_id=user.id,
        device_id=uuid.uuid4(),
    )
    log2 = FeedingLog(
        schedule_id=schedule.id,
        fish_id=fish.id,
        aquarium_id=aquarium.id,
        scheduled_for=datetime(2025, 6, 16, 9, 0),
        action="skipped",
        acted_by_user_id=user.id,
        device_id=uuid.uuid4(),
    )
    async_session.add_all([log1, log2])
    await async_session.commit()

    result = await async_session.execute(
        select(FeedingSchedule).where(FeedingSchedule.id == schedule.id)
    )
    loaded_schedule = result.scalar_one()
    await async_session.refresh(loaded_schedule, ["feeding_logs"])

    assert len(loaded_schedule.feeding_logs) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_schedule_cascade_delete_from_aquarium(async_session: AsyncSession):
    """Test that FeedingSchedule is deleted when Aquarium is deleted."""
    user = User(email="schedule_cascade@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Cascade Schedule Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(
        id="test-cascade-sp",
        common_name="CascadeFish",
        scientific_name="Testus",
        food_types=["flakes"],
        feeding_frequency=1,
        care_level="beginner",
        water_type="freshwater",
    )
    async_session.add(species)
    await async_session.commit()

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
    )
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
async def test_feeding_log_cascade_delete_from_schedule(async_session: AsyncSession):
    """Test that FeedingLog is deleted when FeedingSchedule is deleted."""
    user = User(email="log_cascade_sched@example.com", password_hash="hash")
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    aquarium = Aquarium(owner_id=user.id, name="Cascade Sched Log Tank")
    async_session.add(aquarium)
    await async_session.commit()
    await async_session.refresh(aquarium)

    species = Species(
        id="test-casc-sched-sp",
        common_name="CascSchedFish",
        scientific_name="Testus",
        food_types=["flakes"],
        feeding_frequency=1,
        care_level="beginner",
        water_type="freshwater",
    )
    async_session.add(species)
    await async_session.commit()

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    schedule = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
    )
    async_session.add(schedule)
    await async_session.commit()
    await async_session.refresh(schedule)

    log = FeedingLog(
        schedule_id=schedule.id,
        fish_id=fish.id,
        aquarium_id=aquarium.id,
        scheduled_for=datetime(2025, 6, 15, 9, 0),
        action="fed",
        acted_by_user_id=user.id,
        device_id=uuid.uuid4(),
    )
    async_session.add(log)
    await async_session.commit()

    log_id = log.id
    await async_session.delete(schedule)
    await async_session.commit()

    result = await async_session.execute(select(FeedingLog).where(FeedingLog.id == log_id))
    assert result.scalar_one_or_none() is None


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

    species = Species(
        id="test-aq-rel-sp",
        common_name="AqRelFish",
        scientific_name="Testus",
        food_types=["flakes"],
        feeding_frequency=1,
        care_level="beginner",
        water_type="freshwater",
    )
    async_session.add(species)
    await async_session.commit()

    fish = Fish(aquarium_id=aquarium.id, species_id=species.id)
    async_session.add(fish)
    await async_session.commit()
    await async_session.refresh(fish)

    schedule1 = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
        food_type="flakes",
    )
    schedule2 = FeedingSchedule(
        aquarium_id=aquarium.id,
        fish_id=fish.id,
        time=dt_time(18, 0),
        interval_days=1,
        anchor_date=date.today(),
        food_type="pellets",
    )
    async_session.add_all([schedule1, schedule2])
    await async_session.commit()

    result = await async_session.execute(select(Aquarium).where(Aquarium.id == aquarium.id))
    loaded_aquarium = result.scalar_one()
    await async_session.refresh(loaded_aquarium, ["feeding_schedules"])

    assert len(loaded_aquarium.feeding_schedules) == 2
