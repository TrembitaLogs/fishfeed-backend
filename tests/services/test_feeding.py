"""Integration tests for feeding service (Schedule + FeedingLog architecture)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.species import Species
from app.models.user import User
from app.schemas.feeding import FeedingAction, FeedingLogCreate, ScheduleCreate, ScheduleUpdate
from app.schemas.fish import FishCreate
from app.services.aquarium import AquariumAccessDeniedError
from app.services.feeding import (
    FeedingError,
    FeedingLogConflictError,
    ScheduleNotFoundError,
    _compute_even_times,
    create_feeding_log,
    create_schedule,
    delete_schedule,
    generate_schedule,
    get_feeding_logs,
    get_schedules,
    update_schedule,
)
from app.services.fish import add_fish


async def cleanup_feeding_data(session: AsyncSession) -> None:
    """Helper to cleanup feeding-related data."""
    await session.execute(text("DELETE FROM feeding_logs"))
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
    nickname: str | None = None,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
        nickname=nickname or "TestUser",
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
    portion_hint: str | None = None,
) -> Species:
    """Helper to create a test species."""
    species = Species(
        id=species_id,
        common_name=common_name,
        scientific_name="Test scientific name",
        food_types=food_types or ["flakes"],
        feeding_frequency=feeding_frequency,
        care_level="beginner",
        water_type="freshwater",
        portion_hint=portion_hint,
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


# ──────────────────────────────────────────────
# _compute_even_times unit tests
# ──────────────────────────────────────────────


class TestComputeEvenTimes:
    """Tests for the _compute_even_times helper function."""

    def test_frequency_1_returns_predefined(self):
        assert _compute_even_times(1) == ["09:00"]

    def test_frequency_2_returns_predefined(self):
        assert _compute_even_times(2) == ["09:00", "18:00"]

    def test_frequency_3_returns_predefined(self):
        assert _compute_even_times(3) == ["08:00", "13:00", "18:00"]

    def test_frequency_4_distributes_evenly(self):
        times = _compute_even_times(4)
        assert len(times) == 4
        assert times[0] == "07:00"
        assert times[-1] == "21:00"

    def test_frequency_10_distributes_evenly(self):
        times = _compute_even_times(10)
        assert len(times) == 10
        assert times[0] == "07:00"
        assert times[-1] == "21:00"


# ──────────────────────────────────────────────
# generate_schedule tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestGenerateSchedule:
    """Tests for generate_schedule — per-fish schedule generation."""

    async def test_one_fish_twice_daily_creates_two_schedules(self, async_session: AsyncSession):
        """One fish with frequency=2 should produce 2 FeedingSchedule rows."""
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session, "guppy", "Guppy", feeding_frequency=2)
            aquarium = await create_test_aquarium(async_session, user)
            await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            schedules = await generate_schedule(async_session, aquarium.id, user.id)

            assert len(schedules) == 2
            times = sorted(s.time for s in schedules)
            assert times == [dt_time(9, 0), dt_time(18, 0)]
        finally:
            await cleanup_feeding_data(async_session)

    async def test_two_fish_creates_per_fish_schedules(self, async_session: AsyncSession):
        """Two fish with different frequencies produce independent schedules."""
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            sp1 = await create_test_species(async_session, "guppy", "Guppy", feeding_frequency=1)
            sp2 = await create_test_species(async_session, "betta", "Betta", feeding_frequency=3)
            aquarium = await create_test_aquarium(async_session, user)
            fish1 = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=sp1.id))
            fish2 = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=sp2.id))

            schedules = await generate_schedule(async_session, aquarium.id, user.id)

            # 1 + 3 = 4 schedules total
            assert len(schedules) == 4
            fish1_schedules = [s for s in schedules if s.fish_id == fish1.id]
            fish2_schedules = [s for s in schedules if s.fish_id == fish2.id]
            assert len(fish1_schedules) == 1
            assert len(fish2_schedules) == 3
        finally:
            await cleanup_feeding_data(async_session)

    async def test_no_fish_returns_empty(self, async_session: AsyncSession):
        """Aquarium with no fish should produce zero schedules."""
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user)

            schedules = await generate_schedule(async_session, aquarium.id, user.id)

            assert schedules == []
        finally:
            await cleanup_feeding_data(async_session)

    async def test_food_type_aggregated_from_species(self, async_session: AsyncSession):
        """Schedule food_type should come from species food_types, sorted and joined."""
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(
                async_session,
                "guppy",
                "Guppy",
                feeding_frequency=1,
                food_types=["pellets", "flakes"],
            )
            aquarium = await create_test_aquarium(async_session, user)
            await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            schedules = await generate_schedule(async_session, aquarium.id, user.id)

            assert len(schedules) == 1
            assert schedules[0].food_type == "flakes, pellets"
        finally:
            await cleanup_feeding_data(async_session)

    async def test_portion_hint_from_species(self, async_session: AsyncSession):
        """Schedule should carry portion_hint from species."""
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(
                async_session,
                "guppy",
                "Guppy",
                feeding_frequency=1,
                portion_hint="A pinch",
            )
            aquarium = await create_test_aquarium(async_session, user)
            await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            schedules = await generate_schedule(async_session, aquarium.id, user.id)

            assert schedules[0].portion_hint == "A pinch"
        finally:
            await cleanup_feeding_data(async_session)

    async def test_raises_403_for_non_member(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            owner = await create_test_user(async_session, "owner@example.com")
            other = await create_test_user(async_session, "other@example.com")
            aquarium = await create_test_aquarium(async_session, owner)

            with pytest.raises(AquariumAccessDeniedError) as exc_info:
                await generate_schedule(async_session, aquarium.id, other.id)
            assert exc_info.value.status_code == 403
        finally:
            await cleanup_feeding_data(async_session)

    async def test_idempotent_skips_existing_schedules(self, async_session: AsyncSession):
        """Calling generate_schedule twice should not create duplicate schedules."""
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session, "guppy", "Guppy", feeding_frequency=2)
            aquarium = await create_test_aquarium(async_session, user)
            await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            # First call creates schedules
            schedules1 = await generate_schedule(async_session, aquarium.id, user.id)
            assert len(schedules1) == 2

            # Second call should skip existing and return empty
            schedules2 = await generate_schedule(async_session, aquarium.id, user.id)
            assert len(schedules2) == 0

            # Total schedules in DB should still be 2
            all_schedules = await get_schedules(async_session, aquarium.id, user.id)
            assert len(all_schedules) == 2
        finally:
            await cleanup_feeding_data(async_session)

    async def test_idempotent_creates_only_for_new_fish(self, async_session: AsyncSession):
        """Adding new fish and calling generate_schedule creates schedules only for new fish."""
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            sp1 = await create_test_species(async_session, "guppy", "Guppy", feeding_frequency=2)
            sp2 = await create_test_species(async_session, "betta", "Betta", feeding_frequency=1)
            aquarium = await create_test_aquarium(async_session, user)

            # Add first fish and generate schedules
            fish1 = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=sp1.id))
            schedules1 = await generate_schedule(async_session, aquarium.id, user.id)
            assert len(schedules1) == 2  # guppy has frequency=2

            # Add second fish and generate again
            fish2 = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=sp2.id))
            schedules2 = await generate_schedule(async_session, aquarium.id, user.id)
            assert len(schedules2) == 1  # only betta's schedule (frequency=1)

            # Verify schedules2 belongs to fish2
            assert all(s.fish_id == fish2.id for s in schedules2)

            # Total should be 3
            all_schedules = await get_schedules(async_session, aquarium.id, user.id)
            assert len(all_schedules) == 3
        finally:
            await cleanup_feeding_data(async_session)

    async def test_generate_schedule_uses_utc_date(self, async_session: AsyncSession):
        """Anchor date should use UTC date, not server-local date.

        Simulates a scenario where it's 2026-04-01 00:30 UTC (still March 31
        in UTC-5). The generated schedule should use the UTC date (April 1).
        """
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, user)
            await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            # 2026-04-01 00:30 UTC — in a UTC-5 zone this would still be March 31
            fake_utc_now = datetime(2026, 4, 1, 0, 30, tzinfo=UTC)
            with patch("app.services.feeding.datetime") as mock_dt:
                mock_dt.now.return_value = fake_utc_now
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                schedules = await generate_schedule(async_session, aquarium.id, user.id)

            assert len(schedules) >= 1
            for schedule in schedules:
                assert schedule.anchor_date == date(2026, 4, 1)
        finally:
            await cleanup_feeding_data(async_session)


# ──────────────────────────────────────────────
# get_schedules tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestGetSchedules:
    """Tests for get_schedules — list schedules with optional active filter."""

    async def test_returns_all_schedules(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session, "guppy", "Guppy", feeding_frequency=2)
            aquarium = await create_test_aquarium(async_session, user)
            await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))
            await generate_schedule(async_session, aquarium.id, user.id)

            schedules = await get_schedules(async_session, aquarium.id, user.id)
            assert len(schedules) == 2
        finally:
            await cleanup_feeding_data(async_session)

    async def test_active_filter_true(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session, "guppy", "Guppy", feeding_frequency=2)
            aquarium = await create_test_aquarium(async_session, user)
            fish = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))
            generated = await generate_schedule(async_session, aquarium.id, user.id)

            # Deactivate one
            await update_schedule(
                async_session,
                aquarium.id,
                generated[0].id,
                user.id,
                ScheduleUpdate(active=False),
            )

            active = await get_schedules(async_session, aquarium.id, user.id, active=True)
            assert len(active) == 1

            all_schedules = await get_schedules(async_session, aquarium.id, user.id)
            assert len(all_schedules) == 2
        finally:
            await cleanup_feeding_data(async_session)

    async def test_empty_aquarium_returns_empty(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user)

            schedules = await get_schedules(async_session, aquarium.id, user.id)
            assert schedules == []
        finally:
            await cleanup_feeding_data(async_session)

    async def test_raises_403_for_non_member(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            owner = await create_test_user(async_session, "owner@example.com")
            other = await create_test_user(async_session, "other@example.com")
            aquarium = await create_test_aquarium(async_session, owner)

            with pytest.raises(AquariumAccessDeniedError):
                await get_schedules(async_session, aquarium.id, other.id)
        finally:
            await cleanup_feeding_data(async_session)


# ──────────────────────────────────────────────
# create_schedule tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestCreateSchedule:
    """Tests for create_schedule — single schedule creation with validation."""

    async def test_creates_schedule_successfully(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, user)
            fish = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="10:30",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )
            schedule = await create_schedule(async_session, aquarium.id, user.id, data)

            assert schedule.fish_id == fish.id
            assert schedule.time == dt_time(10, 30)
            assert schedule.interval_days == 1
            assert schedule.active is True
            assert schedule.created_by_user_id == user.id
        finally:
            await cleanup_feeding_data(async_session)

    async def test_rejects_fish_not_in_aquarium(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session)
            aq1 = await create_test_aquarium(async_session, user, "AQ1")
            aq2 = await create_test_aquarium(async_session, user, "AQ2")
            fish = await add_fish(async_session, aq1.id, user.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="09:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )
            with pytest.raises(FeedingError) as exc_info:
                await create_schedule(async_session, aq2.id, user.id, data)
            assert exc_info.value.status_code == 400
        finally:
            await cleanup_feeding_data(async_session)

    async def test_rejects_anchor_date_too_far_in_future(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, user)
            fish = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="09:00",
                interval_days=1,
                anchor_date=date.today() + timedelta(days=8),
                food_type="flakes",
            )
            with pytest.raises(FeedingError) as exc_info:
                await create_schedule(async_session, aquarium.id, user.id, data)
            assert exc_info.value.status_code == 400
            assert "7 days" in exc_info.value.message
        finally:
            await cleanup_feeding_data(async_session)

    async def test_raises_403_for_non_member(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            owner = await create_test_user(async_session, "owner@example.com")
            other = await create_test_user(async_session, "other@example.com")
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, owner)
            fish = await add_fish(async_session, aquarium.id, owner.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="09:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )
            with pytest.raises(AquariumAccessDeniedError):
                await create_schedule(async_session, aquarium.id, other.id, data)
        finally:
            await cleanup_feeding_data(async_session)


# ──────────────────────────────────────────────
# update_schedule tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestUpdateSchedule:
    """Tests for update_schedule — partial update by schedule ID."""

    async def test_updates_time_and_food_type(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, user)
            fish = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="09:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )
            schedule = await create_schedule(async_session, aquarium.id, user.id, data)

            updated = await update_schedule(
                async_session,
                aquarium.id,
                schedule.id,
                user.id,
                ScheduleUpdate(time="14:00", food_type="pellets"),
            )

            assert updated.time == dt_time(14, 0)
            assert updated.food_type == "pellets"
        finally:
            await cleanup_feeding_data(async_session)

    async def test_deactivates_schedule(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, user)
            fish = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="09:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )
            schedule = await create_schedule(async_session, aquarium.id, user.id, data)

            updated = await update_schedule(
                async_session,
                aquarium.id,
                schedule.id,
                user.id,
                ScheduleUpdate(active=False),
            )
            assert updated.active is False
        finally:
            await cleanup_feeding_data(async_session)

    async def test_raises_404_for_nonexistent(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user)

            with pytest.raises(ScheduleNotFoundError) as exc_info:
                await update_schedule(
                    async_session,
                    aquarium.id,
                    uuid.uuid4(),
                    user.id,
                    ScheduleUpdate(food_type="pellets"),
                )
            assert exc_info.value.status_code == 404
        finally:
            await cleanup_feeding_data(async_session)

    async def test_rejects_anchor_date_too_far(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, user)
            fish = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="09:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )
            schedule = await create_schedule(async_session, aquarium.id, user.id, data)

            with pytest.raises(FeedingError) as exc_info:
                await update_schedule(
                    async_session,
                    aquarium.id,
                    schedule.id,
                    user.id,
                    ScheduleUpdate(anchor_date=date.today() + timedelta(days=8)),
                )
            assert exc_info.value.status_code == 400
        finally:
            await cleanup_feeding_data(async_session)


# ──────────────────────────────────────────────
# delete_schedule tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestDeleteSchedule:
    """Tests for delete_schedule — hard delete by ID."""

    async def test_deletes_successfully(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, user)
            fish = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="09:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )
            schedule = await create_schedule(async_session, aquarium.id, user.id, data)

            await delete_schedule(async_session, aquarium.id, schedule.id, user.id)

            remaining = await get_schedules(async_session, aquarium.id, user.id)
            assert len(remaining) == 0
        finally:
            await cleanup_feeding_data(async_session)

    async def test_raises_404_for_nonexistent(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user)

            with pytest.raises(ScheduleNotFoundError):
                await delete_schedule(async_session, aquarium.id, uuid.uuid4(), user.id)
        finally:
            await cleanup_feeding_data(async_session)

    async def test_raises_403_for_non_member(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            owner = await create_test_user(async_session, "owner@example.com")
            other = await create_test_user(async_session, "other@example.com")
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, owner)
            fish = await add_fish(async_session, aquarium.id, owner.id, FishCreate(species_id=species.id))

            data = ScheduleCreate(
                fish_id=fish.id,
                time="09:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )
            schedule = await create_schedule(async_session, aquarium.id, owner.id, data)

            with pytest.raises(AquariumAccessDeniedError):
                await delete_schedule(async_session, aquarium.id, schedule.id, other.id)
        finally:
            await cleanup_feeding_data(async_session)


# ──────────────────────────────────────────────
# get_feeding_logs tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestGetFeedingLogs:
    """Tests for get_feeding_logs — list logs with date range and fish filter."""

    async def _setup(self, session: AsyncSession):
        """Create standard test data and return (user, aquarium, fish, schedule)."""
        user = await create_test_user(session)
        species = await create_test_species(session)
        aquarium = await create_test_aquarium(session, user)
        fish = await add_fish(session, aquarium.id, user.id, FishCreate(species_id=species.id))
        generated = await generate_schedule(session, aquarium.id, user.id)
        return user, aquarium, fish, generated[0]

    async def test_returns_logs_in_date_range(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user, aquarium, fish, schedule = await self._setup(async_session)
            device_id = uuid.uuid4()

            # Create a log for today
            now = datetime.now()
            log_data = FeedingLogCreate(
                schedule_id=schedule.id,
                fish_id=fish.id,
                scheduled_for=now,
                action=FeedingAction.fed,
                device_id=device_id,
            )
            await create_feeding_log(async_session, aquarium.id, user.id, log_data)

            from_dt = datetime.combine(date.today(), dt_time.min)
            to_dt = datetime.combine(date.today(), dt_time.max)
            logs = await get_feeding_logs(async_session, aquarium.id, user.id, from_dt, to_dt)

            assert len(logs) == 1
            assert logs[0].action == "fed"
        finally:
            await cleanup_feeding_data(async_session)

    async def test_fish_id_filter(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user = await create_test_user(async_session)
            sp1 = await create_test_species(async_session, "guppy", "Guppy", feeding_frequency=1)
            sp2 = await create_test_species(async_session, "betta", "Betta", feeding_frequency=1)
            aquarium = await create_test_aquarium(async_session, user)
            fish1 = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=sp1.id))
            fish2 = await add_fish(async_session, aquarium.id, user.id, FishCreate(species_id=sp2.id))

            schedules = await generate_schedule(async_session, aquarium.id, user.id)
            device_id = uuid.uuid4()
            now = datetime.now()

            # Create logs for both fish
            for s in schedules:
                log_data = FeedingLogCreate(
                    schedule_id=s.id,
                    fish_id=s.fish_id,
                    scheduled_for=now,
                    action=FeedingAction.fed,
                    device_id=device_id,
                )
                await create_feeding_log(async_session, aquarium.id, user.id, log_data)

            from_dt = datetime.combine(date.today(), dt_time.min)
            to_dt = datetime.combine(date.today(), dt_time.max)

            logs_fish1 = await get_feeding_logs(
                async_session,
                aquarium.id,
                user.id,
                from_dt,
                to_dt,
                fish_id=fish1.id,
            )
            assert len(logs_fish1) == 1
            assert logs_fish1[0].fish_id == fish1.id
        finally:
            await cleanup_feeding_data(async_session)

    async def test_raises_403_for_non_member(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            owner = await create_test_user(async_session, "owner@example.com")
            other = await create_test_user(async_session, "other@example.com")
            aquarium = await create_test_aquarium(async_session, owner)

            from_dt = datetime.combine(date.today(), dt_time.min)
            to_dt = datetime.combine(date.today(), dt_time.max)

            with pytest.raises(AquariumAccessDeniedError):
                await get_feeding_logs(async_session, aquarium.id, other.id, from_dt, to_dt)
        finally:
            await cleanup_feeding_data(async_session)


# ──────────────────────────────────────────────
# create_feeding_log tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestCreateFeedingLog:
    """Tests for create_feeding_log — with duplicate detection (409)."""

    async def _setup(self, session: AsyncSession):
        user = await create_test_user(session)
        species = await create_test_species(session)
        aquarium = await create_test_aquarium(session, user)
        fish = await add_fish(session, aquarium.id, user.id, FishCreate(species_id=species.id))
        generated = await generate_schedule(session, aquarium.id, user.id)
        return user, aquarium, fish, generated[0]

    async def test_creates_fed_log(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user, aquarium, fish, schedule = await self._setup(async_session)
            device_id = uuid.uuid4()
            now = datetime.now()

            log = await create_feeding_log(
                async_session,
                aquarium.id,
                user.id,
                FeedingLogCreate(
                    schedule_id=schedule.id,
                    fish_id=fish.id,
                    scheduled_for=now,
                    action=FeedingAction.fed,
                    device_id=device_id,
                ),
            )

            assert log.action == "fed"
            assert log.acted_by_user_id == user.id
            assert log.aquarium_id == aquarium.id
            assert log.schedule_id == schedule.id
        finally:
            await cleanup_feeding_data(async_session)

    async def test_creates_skipped_log(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user, aquarium, fish, schedule = await self._setup(async_session)

            log = await create_feeding_log(
                async_session,
                aquarium.id,
                user.id,
                FeedingLogCreate(
                    schedule_id=schedule.id,
                    fish_id=fish.id,
                    scheduled_for=datetime.now(),
                    action=FeedingAction.skipped,
                    device_id=uuid.uuid4(),
                ),
            )
            assert log.action == "skipped"
        finally:
            await cleanup_feeding_data(async_session)

    async def test_duplicate_raises_409_conflict(self, async_session: AsyncSession):
        """UNIQUE(schedule_id, scheduled_for) constraint triggers 409."""
        await cleanup_feeding_data(async_session)
        try:
            user, aquarium, fish, schedule = await self._setup(async_session)
            device_id = uuid.uuid4()
            scheduled_for = datetime(2025, 6, 15, 9, 0)

            log_data = FeedingLogCreate(
                schedule_id=schedule.id,
                fish_id=fish.id,
                scheduled_for=scheduled_for,
                action=FeedingAction.fed,
                device_id=device_id,
            )

            # First create succeeds
            await create_feeding_log(async_session, aquarium.id, user.id, log_data)

            # Second create with same schedule_id + scheduled_for raises conflict
            with pytest.raises(FeedingLogConflictError) as exc_info:
                await create_feeding_log(async_session, aquarium.id, user.id, log_data)

            assert exc_info.value.status_code == 409
            assert exc_info.value.existing_log is not None
        finally:
            await cleanup_feeding_data(async_session)

    async def test_log_with_notes(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            user, aquarium, fish, schedule = await self._setup(async_session)

            log = await create_feeding_log(
                async_session,
                aquarium.id,
                user.id,
                FeedingLogCreate(
                    schedule_id=schedule.id,
                    fish_id=fish.id,
                    scheduled_for=datetime.now(),
                    action=FeedingAction.fed,
                    device_id=uuid.uuid4(),
                    notes="Extra portion today",
                ),
            )
            assert log.notes == "Extra portion today"
        finally:
            await cleanup_feeding_data(async_session)

    async def test_member_can_create_log(self, async_session: AsyncSession):
        """Aquarium member (not just owner) should be able to create logs."""
        await cleanup_feeding_data(async_session)
        try:
            owner = await create_test_user(async_session, "owner@example.com")
            member_user = await create_test_user(async_session, "member@example.com")
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, owner)

            # Add member
            member = AquariumMember(
                aquarium_id=aquarium.id,
                user_id=member_user.id,
                role="member",
            )
            async_session.add(member)
            await async_session.commit()

            fish = await add_fish(async_session, aquarium.id, owner.id, FishCreate(species_id=species.id))
            generated = await generate_schedule(async_session, aquarium.id, owner.id)

            log = await create_feeding_log(
                async_session,
                aquarium.id,
                member_user.id,
                FeedingLogCreate(
                    schedule_id=generated[0].id,
                    fish_id=fish.id,
                    scheduled_for=datetime.now(),
                    action=FeedingAction.fed,
                    device_id=uuid.uuid4(),
                ),
            )
            assert log.acted_by_user_id == member_user.id
        finally:
            await cleanup_feeding_data(async_session)

    async def test_raises_403_for_non_member(self, async_session: AsyncSession):
        await cleanup_feeding_data(async_session)
        try:
            owner = await create_test_user(async_session, "owner@example.com")
            other = await create_test_user(async_session, "other@example.com")
            species = await create_test_species(async_session)
            aquarium = await create_test_aquarium(async_session, owner)
            fish = await add_fish(async_session, aquarium.id, owner.id, FishCreate(species_id=species.id))
            generated = await generate_schedule(async_session, aquarium.id, owner.id)

            with pytest.raises(AquariumAccessDeniedError):
                await create_feeding_log(
                    async_session,
                    aquarium.id,
                    other.id,
                    FeedingLogCreate(
                        schedule_id=generated[0].id,
                        fish_id=fish.id,
                        scheduled_for=datetime.now(),
                        action=FeedingAction.fed,
                        device_id=uuid.uuid4(),
                    ),
                )
        finally:
            await cleanup_feeding_data(async_session)
