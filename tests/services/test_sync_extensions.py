"""Unit tests for sync extensions: fish notes, fish move, aquarium water_type/capacity, serialization."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingSchedule
from app.models.fish import Fish
from app.models.species import Species
from app.models.user import User
from app.schemas.sync import ChangeItem
from app.services.sync.changes import _apply_aquarium_change, _apply_fish_change
from app.services.sync.utils import _entity_to_dict

# ============================================================================
# Helpers — mirror patterns from test_sync.py
# ============================================================================


async def cleanup_data(session: AsyncSession) -> None:
    """Delete all test data in correct FK order."""
    await session.execute(text("DELETE FROM orphaned_images"))
    await session.execute(text("DELETE FROM feeding_logs"))
    await session.execute(text("DELETE FROM feeding_schedules"))
    await session.execute(text("DELETE FROM fish"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM family_invites"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
    await session.commit()


async def make_user(session: AsyncSession, email: str | None = None) -> User:
    """Create and return a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def make_aquarium(
    session: AsyncSession,
    owner_id: uuid.UUID,
    name: str = "Test Aquarium",
    water_type: str | None = None,
    capacity: float | None = None,
) -> Aquarium:
    """Create aquarium with owner as member."""
    aquarium = Aquarium(
        owner_id=owner_id,
        name=name,
        water_type=water_type,
        capacity=capacity,
    )
    session.add(aquarium)
    await session.flush()

    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=owner_id,
        role="owner",
    )
    session.add(member)
    await session.commit()
    await session.refresh(aquarium)
    return aquarium


async def ensure_species(session: AsyncSession, species_id: str = "test-guppy") -> Species:
    """Ensure species exists, create if needed."""
    stmt = select(Species).where(Species.id == species_id)
    result = await session.execute(stmt)
    species = result.scalar_one_or_none()
    if species is None:
        species = Species(
            id=species_id,
            common_name="Test Guppy",
            scientific_name="Poecilia reticulata",
            food_types=["flakes"],
            feeding_frequency=2,
            care_level="beginner",
            water_type="freshwater",
        )
        session.add(species)
        await session.commit()
        await session.refresh(species)
    return species


async def make_fish(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
    species_id: str = "test-guppy",
    notes: str | None = None,
) -> Fish:
    """Create and return a test fish."""
    await ensure_species(session, species_id)
    fish = Fish(
        aquarium_id=aquarium_id,
        species_id=species_id,
        quantity=1,
        notes=notes,
    )
    session.add(fish)
    await session.commit()
    await session.refresh(fish)
    return fish


async def make_schedule(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
    fish_id: uuid.UUID,
    user_id: uuid.UUID,
    schedule_time: time | None = None,
) -> FeedingSchedule:
    """Create and return a test feeding schedule."""
    schedule = FeedingSchedule(
        aquarium_id=aquarium_id,
        fish_id=fish_id,
        time=schedule_time or time(9, 0),
        interval_days=1,
        anchor_date=date.today(),
        food_type="flakes",
        active=True,
        created_by_user_id=user_id,
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule


# ============================================================================
# 12.1 — TestFishSyncExtensions
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
class TestFishSyncExtensions:
    """Tests for fish sync handler: notes field and aquarium move."""

    async def test_fish_create_with_notes(self, async_session: AsyncSession):
        """CREATE fish with notes field — notes saved correctly."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)
            await ensure_species(async_session)

            fish_id = uuid.uuid4()
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium.id),
                    "species_id": "test-guppy",
                    "notes": "Loves bloodworms",
                },
                client_updated_at=datetime.now(UTC),
            )

            conflict = await _apply_fish_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            fish = await async_session.get(Fish, fish_id)
            assert fish is not None
            assert fish.notes == "Loves bloodworms"
        finally:
            await cleanup_data(async_session)

    async def test_fish_update_notes(self, async_session: AsyncSession):
        """UPDATE existing fish notes — notes updated."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)
            fish = await make_fish(async_session, aquarium.id, notes="Old note")

            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"notes": "New note"},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(fish)
            assert fish.notes == "New note"
        finally:
            await cleanup_data(async_session)

    async def test_fish_update_notes_null(self, async_session: AsyncSession):
        """UPDATE fish notes to null — notes cleared."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)
            fish = await make_fish(async_session, aquarium.id, notes="Some note")

            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"notes": None},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(fish)
            assert fish.notes is None
        finally:
            await cleanup_data(async_session)

    async def test_fish_update_notes_truncation(self, async_session: AsyncSession):
        """UPDATE fish notes > 500 chars — truncated to 500."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)
            fish = await make_fish(async_session, aquarium.id)

            long_notes = "x" * 600
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"notes": long_notes},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(fish)
            assert fish.notes is not None
            assert len(fish.notes) == 500
        finally:
            await cleanup_data(async_session)

    async def test_fish_create_notes_truncation(self, async_session: AsyncSession):
        """CREATE fish with notes > 500 chars — truncated on create too."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)
            await ensure_species(async_session)

            fish_id = uuid.uuid4()
            long_notes = "a" * 700
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium.id),
                    "species_id": "test-guppy",
                    "notes": long_notes,
                },
                client_updated_at=datetime.now(UTC),
            )

            conflict = await _apply_fish_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            fish = await async_session.get(Fish, fish_id)
            assert fish is not None
            assert fish.notes is not None
            assert len(fish.notes) == 500
        finally:
            await cleanup_data(async_session)

    async def test_fish_move_to_own_aquarium(self, async_session: AsyncSession):
        """Move fish to user's own aquarium — success + schedules updated."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium_a = await make_aquarium(async_session, user.id, name="Tank A")
            aquarium_b = await make_aquarium(async_session, user.id, name="Tank B")
            fish = await make_fish(async_session, aquarium_a.id)
            schedule = await make_schedule(async_session, aquarium_a.id, fish.id, user.id)

            accessible = {aquarium_a.id, aquarium_b.id}
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"aquarium_id": str(aquarium_b.id)},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, user.id, change, accessible)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(fish)
            assert fish.aquarium_id == aquarium_b.id

            # Verify schedule was atomically updated
            await async_session.refresh(schedule)
            assert schedule.aquarium_id == aquarium_b.id
        finally:
            await cleanup_data(async_session)

    async def test_fish_move_to_family_member_aquarium(self, async_session: AsyncSession):
        """Move fish to family member's aquarium — success when in accessible set."""
        await cleanup_data(async_session)
        try:
            owner = await make_user(async_session, "owner@example.com")
            member = await make_user(async_session, "member@example.com")
            aquarium_owner = await make_aquarium(async_session, owner.id, name="Owner Tank")

            # Make member an aquarium member of owner's aquarium
            am = AquariumMember(
                aquarium_id=aquarium_owner.id,
                user_id=member.id,
                role="member",
            )
            async_session.add(am)

            # Member also has own aquarium with fish
            aquarium_member = await make_aquarium(async_session, member.id, name="Member Tank")
            fish = await make_fish(async_session, aquarium_member.id)

            # Member can access both aquariums
            accessible = {aquarium_owner.id, aquarium_member.id}
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"aquarium_id": str(aquarium_owner.id)},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, member.id, change, accessible)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(fish)
            assert fish.aquarium_id == aquarium_owner.id
        finally:
            await cleanup_data(async_session)

    async def test_fish_move_to_foreign_aquarium_rejected(self, async_session: AsyncSession):
        """Move fish to foreign aquarium — rejected (warning logged, no move)."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session, "user@example.com")
            foreign_user = await make_user(async_session, "foreign@example.com")
            aquarium_user = await make_aquarium(async_session, user.id, name="User Tank")
            aquarium_foreign = await make_aquarium(async_session, foreign_user.id, name="Foreign Tank")
            fish = await make_fish(async_session, aquarium_user.id)

            # User does NOT have access to foreign aquarium
            accessible = {aquarium_user.id}
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"aquarium_id": str(aquarium_foreign.id)},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, user.id, change, accessible)
            await async_session.flush()

            # No conflict returned, but move is silently skipped
            assert conflict is None
            await async_session.refresh(fish)
            assert fish.aquarium_id == aquarium_user.id  # Fish stayed in original aquarium
        finally:
            await cleanup_data(async_session)

    async def test_fish_move_to_soft_deleted_aquarium_rejected(self, async_session: AsyncSession):
        """Move fish to soft-deleted aquarium — rejected."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium_a = await make_aquarium(async_session, user.id, name="Tank A")
            aquarium_b = await make_aquarium(async_session, user.id, name="Tank B (Deleted)")
            fish = await make_fish(async_session, aquarium_a.id)

            # Soft-delete target aquarium
            aquarium_b.deleted_at = datetime.now(UTC)
            await async_session.commit()

            accessible = {aquarium_a.id, aquarium_b.id}
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"aquarium_id": str(aquarium_b.id)},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, user.id, change, accessible)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(fish)
            assert fish.aquarium_id == aquarium_a.id  # Fish stayed
        finally:
            await cleanup_data(async_session)

    async def test_fish_move_same_aquarium_id_no_action(self, async_session: AsyncSession):
        """Move fish with same aquarium_id — no action (race condition protection)."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)
            fish = await make_fish(async_session, aquarium.id)
            schedule = await make_schedule(async_session, aquarium.id, fish.id, user.id)

            accessible = {aquarium.id}
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"aquarium_id": str(aquarium.id)},  # Same aquarium
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, user.id, change, accessible)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(fish)
            assert fish.aquarium_id == aquarium.id

            # Schedule should still be on original aquarium (no unnecessary update)
            await async_session.refresh(schedule)
            assert schedule.aquarium_id == aquarium.id
        finally:
            await cleanup_data(async_session)

    async def test_feeding_schedule_atomic_update_on_fish_move(self, async_session: AsyncSession):
        """After fish move, ALL schedules for that fish have the new aquarium_id."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium_a = await make_aquarium(async_session, user.id, name="Tank A")
            aquarium_b = await make_aquarium(async_session, user.id, name="Tank B")
            fish = await make_fish(async_session, aquarium_a.id)

            # Create multiple schedules for the same fish
            sched1 = await make_schedule(async_session, aquarium_a.id, fish.id, user.id, time(8, 0))
            sched2 = await make_schedule(async_session, aquarium_a.id, fish.id, user.id, time(12, 0))
            sched3 = await make_schedule(async_session, aquarium_a.id, fish.id, user.id, time(18, 0))

            accessible = {aquarium_a.id, aquarium_b.id}
            change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"aquarium_id": str(aquarium_b.id)},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_fish_change(async_session, user.id, change, accessible)
            await async_session.flush()

            assert conflict is None

            # Verify all schedules moved
            for sched in [sched1, sched2, sched3]:
                await async_session.refresh(sched)
                assert sched.aquarium_id == aquarium_b.id
        finally:
            await cleanup_data(async_session)


# ============================================================================
# 12.2 — TestAquariumSyncExtensions
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
class TestAquariumSyncExtensions:
    """Tests for aquarium sync handler: water_type and capacity fields."""

    async def test_aquarium_create_with_water_type_and_capacity(self, async_session: AsyncSession):
        """CREATE aquarium with water_type and capacity — both saved."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium_id = uuid.uuid4()

            change = ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium_id,
                operation="create",
                data={
                    "name": "Reef Tank",
                    "water_type": "saltwater",
                    "capacity": 150.5,
                },
                client_updated_at=datetime.now(UTC),
            )

            conflict = await _apply_aquarium_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            aquarium = await async_session.get(Aquarium, aquarium_id)
            assert aquarium is not None
            assert aquarium.water_type == "saltwater"
            assert aquarium.capacity is not None
            assert float(aquarium.capacity) == pytest.approx(150.5)
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_update_water_type(self, async_session: AsyncSession):
        """UPDATE aquarium water_type — updated correctly."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id, water_type="freshwater")

            change = ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"water_type": "brackish"},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_aquarium_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(aquarium)
            assert aquarium.water_type == "brackish"
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_update_invalid_water_type_fallback(self, async_session: AsyncSession):
        """UPDATE aquarium with invalid water_type — falls back to 'freshwater'."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id, water_type="saltwater")

            change = ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"water_type": "invalid_type"},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_aquarium_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(aquarium)
            assert aquarium.water_type == "freshwater"
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_update_capacity(self, async_session: AsyncSession):
        """UPDATE aquarium capacity — updated correctly."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)

            change = ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"capacity": 200.75},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_aquarium_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(aquarium)
            assert aquarium.capacity is not None
            assert float(aquarium.capacity) == pytest.approx(200.75)
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_update_capacity_null(self, async_session: AsyncSession):
        """UPDATE aquarium capacity to null — cleared."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id, capacity=100.0)

            change = ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"capacity": None},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_aquarium_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(aquarium)
            assert aquarium.capacity is None
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_update_capacity_negative_skipped(self, async_session: AsyncSession):
        """UPDATE aquarium with negative capacity — skipped with warning."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id, capacity=100.0)

            change = ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"capacity": -50},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_aquarium_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(aquarium)
            # Capacity should remain unchanged
            assert aquarium.capacity is not None
            assert float(aquarium.capacity) == pytest.approx(100.0)
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_update_capacity_zero_skipped(self, async_session: AsyncSession):
        """UPDATE aquarium with capacity=0 — skipped with warning."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id, capacity=100.0)

            change = ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"capacity": 0},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=10),
            )

            conflict = await _apply_aquarium_change(async_session, user.id, change)
            await async_session.flush()

            assert conflict is None
            await async_session.refresh(aquarium)
            # Capacity should remain unchanged
            assert aquarium.capacity is not None
            assert float(aquarium.capacity) == pytest.approx(100.0)
        finally:
            await cleanup_data(async_session)


# ============================================================================
# 12.3 — TestEntitySerialization
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
class TestEntitySerialization:
    """Tests for _entity_to_dict serialization of extended fields."""

    async def test_fish_serialization_includes_notes(self, async_session: AsyncSession):
        """Fish serialization includes notes field."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)
            fish = await make_fish(async_session, aquarium.id, notes="Friendly fish")

            result = _entity_to_dict(fish)

            assert "notes" in result
            assert result["notes"] == "Friendly fish"
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_serialization_includes_water_type(self, async_session: AsyncSession):
        """Aquarium serialization includes water_type field."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id, water_type="saltwater")

            result = _entity_to_dict(aquarium)

            assert "water_type" in result
            assert result["water_type"] == "saltwater"
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_serialization_capacity_as_float(self, async_session: AsyncSession):
        """Aquarium serialization converts Decimal capacity to float."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id, capacity=250.5)

            result = _entity_to_dict(aquarium)

            assert "capacity" in result
            assert isinstance(result["capacity"], float)
            assert result["capacity"] == pytest.approx(250.5)
        finally:
            await cleanup_data(async_session)

    async def test_aquarium_serialization_capacity_none(self, async_session: AsyncSession):
        """Aquarium serialization with capacity=None — returns None, no error."""
        await cleanup_data(async_session)
        try:
            user = await make_user(async_session)
            aquarium = await make_aquarium(async_session, user.id)

            result = _entity_to_dict(aquarium)

            assert "capacity" in result
            assert result["capacity"] is None
        finally:
            await cleanup_data(async_session)
