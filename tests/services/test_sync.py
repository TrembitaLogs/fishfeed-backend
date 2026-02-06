"""Integration tests for sync service."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.models.species import Species
from app.models.user import User
from app.schemas.sync import ChangeItem, SyncRequest
from app.services.sync import (
    SyncAccessDeniedError,
    SyncValidationError,
    _ensure_schedules_for_user,
    process_sync,
)


async def cleanup_sync_test_data(session: AsyncSession) -> None:
    """Helper to cleanup sync test data."""
    await session.execute(text("DELETE FROM feeding_logs"))
    await session.execute(text("DELETE FROM feeding_schedules"))
    await session.execute(text("DELETE FROM fish"))
    await session.execute(text("DELETE FROM aquarium_members"))
    await session.execute(text("DELETE FROM family_invites"))
    await session.execute(text("DELETE FROM aquariums"))
    await session.execute(text("DELETE FROM users"))
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


async def create_test_aquarium(
    session: AsyncSession,
    owner_id: uuid.UUID,
    name: str = "Test Aquarium",
) -> Aquarium:
    """Helper to create a test aquarium with owner as member."""
    aquarium = Aquarium(
        owner_id=owner_id,
        name=name,
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


async def ensure_test_species(
    session: AsyncSession,
    species_id: str = "test-guppy",
) -> Species:
    """Ensure test species exists, create if not."""
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


async def create_test_fish(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
    species_id: str = "test-guppy",
) -> Fish:
    """Helper to create a test fish."""
    await ensure_test_species(session, species_id)
    fish = Fish(
        aquarium_id=aquarium_id,
        species_id=species_id,
        quantity=1,
    )
    session.add(fish)
    await session.commit()
    await session.refresh(fish)
    return fish


async def create_test_schedule(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
    fish_id: uuid.UUID,
    user_id: uuid.UUID,
    schedule_time: time | None = None,
) -> FeedingSchedule:
    """Helper to create a test feeding schedule."""
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


async def create_test_feeding_log(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    fish_id: uuid.UUID,
    aquarium_id: uuid.UUID,
    user_id: uuid.UUID,
    scheduled_for: datetime | None = None,
    action: str = "fed",
) -> FeedingLog:
    """Helper to create a test feeding log."""
    log = FeedingLog(
        schedule_id=schedule_id,
        fish_id=fish_id,
        aquarium_id=aquarium_id,
        scheduled_for=scheduled_for or datetime.now(UTC).replace(tzinfo=None),
        action=action,
        acted_at=datetime.now(UTC),
        acted_by_user_id=user_id,
        device_id=uuid.uuid4(),
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


# ============================================================================
# process_sync tests - empty changes
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_empty_changes_returns_server_state(
    async_session: AsyncSession,
):
    """Test that sync with empty changes returns server state."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        request = SyncRequest(changes=[], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.server_state is not None
        assert response.conflicts == []
        assert response.sync_token is not None
        assert len(response.sync_token) > 0
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_generates_unique_sync_tokens(
    async_session: AsyncSession,
):
    """Test that each sync generates a unique sync token."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        request = SyncRequest(changes=[], last_sync_at=None)

        response1 = await process_sync(async_session, user.id, request)
        response2 = await process_sync(async_session, user.id, request)

        assert response1.sync_token != response2.sync_token
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_with_last_sync_at(
    async_session: AsyncSession,
):
    """Test that sync with last_sync_at processes correctly (delta sync)."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        last_sync = datetime.now(UTC) - timedelta(hours=1)
        request = SyncRequest(changes=[], last_sync_at=last_sync)

        response = await process_sync(async_session, user.id, request)

        assert response.server_state is not None
        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Entity ownership validation tests - aquarium
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_create_aquarium(
    async_session: AsyncSession,
):
    """Test that create aquarium operation is always allowed."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        new_aquarium_id = uuid.uuid4()
        change = ChangeItem(
            entity_type="aquarium",
            entity_id=new_aquarium_id,
            operation="create",
            data={"name": "New Aquarium"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_update_owned_aquarium(
    async_session: AsyncSession,
):
    """Test that update aquarium is allowed for owned aquariums."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        change = ChangeItem(
            entity_type="aquarium",
            entity_id=aquarium.id,
            operation="update",
            data={"name": "Updated Name"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_denies_update_other_user_aquarium(
    async_session: AsyncSession,
):
    """Test that update aquarium is denied for other user's aquariums."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)

        change = ChangeItem(
            entity_type="aquarium",
            entity_id=aquarium.id,
            operation="update",
            data={"name": "Hacked"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncAccessDeniedError) as exc_info:
            await process_sync(async_session, other_user.id, request)

        assert exc_info.value.status_code == 403
        assert "aquarium" in exc_info.value.message
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Entity ownership validation tests - fish
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_create_fish_in_owned_aquarium(
    async_session: AsyncSession,
):
    """Test that create fish is allowed in owned aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        await ensure_test_species(async_session)

        change = ChangeItem(
            entity_type="fish",
            entity_id=uuid.uuid4(),
            operation="create",
            data={"aquarium_id": str(aquarium.id), "species_id": "test-guppy"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_denies_create_fish_in_other_user_aquarium(
    async_session: AsyncSession,
):
    """Test that create fish is denied in other user's aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)

        change = ChangeItem(
            entity_type="fish",
            entity_id=uuid.uuid4(),
            operation="create",
            data={"aquarium_id": str(aquarium.id), "species_id": "test-guppy"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncAccessDeniedError) as exc_info:
            await process_sync(async_session, other_user.id, request)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_denies_create_fish_missing_aquarium_id(
    async_session: AsyncSession,
):
    """Test that create fish without aquarium_id raises validation error."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)

        change = ChangeItem(
            entity_type="fish",
            entity_id=uuid.uuid4(),
            operation="create",
            data={"species_id": "test-guppy"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncValidationError) as exc_info:
            await process_sync(async_session, user.id, request)

        assert "aquarium_id" in exc_info.value.message
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Entity ownership validation tests - feeding_log
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_create_feeding_log_in_owned_aquarium(
    async_session: AsyncSession,
):
    """Test that create feeding_log is allowed in owned aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=uuid.uuid4(),
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "fish_id": str(fish.id),
                "scheduled_for": datetime.now(UTC).isoformat(),
                "action": "fed",
                "device_id": str(uuid.uuid4()),
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
        assert response.conflicts == []
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_denies_create_feeding_log_in_other_user_aquarium(
    async_session: AsyncSession,
):
    """Test that create feeding_log is denied in other user's aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)

        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=uuid.uuid4(),
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "scheduled_for": datetime.now(UTC).isoformat(),
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncAccessDeniedError) as exc_info:
            await process_sync(async_session, other_user.id, request)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_denies_create_feeding_log_missing_aquarium_id(
    async_session: AsyncSession,
):
    """Test that create feeding_log without aquarium_id raises validation error."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)

        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=uuid.uuid4(),
            operation="create",
            data={"scheduled_for": datetime.now(UTC).isoformat()},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncValidationError) as exc_info:
            await process_sync(async_session, user.id, request)

        assert "aquarium_id" in exc_info.value.message
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Cascading creates in a single batch
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_batch_aquarium_fish_schedule_creates(
    async_session: AsyncSession,
):
    """Test that a batch with aquarium + fish + schedule creates works.

    The mobile client sends all three in a single sync batch.
    The server must accept fish/schedule that reference an aquarium
    being created in the same batch.
    """
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        await ensure_test_species(async_session)

        aquarium_id = uuid.uuid4()
        fish_id = uuid.uuid4()
        schedule_id = uuid.uuid4()
        now = datetime.now(UTC)

        changes = [
            ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium_id,
                operation="create",
                data={"name": "New Tank"},
                client_updated_at=now,
            ),
            ChangeItem(
                entity_type="fish",
                entity_id=fish_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium_id),
                    "species_id": "test-guppy",
                    "quantity": 3,
                },
                client_updated_at=now,
            ),
            ChangeItem(
                entity_type="schedule",
                entity_id=schedule_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium_id),
                    "fish_id": str(fish_id),
                    "time": "09:00",
                    "food_type": "flakes",
                },
                client_updated_at=now,
            ),
        ]
        request = SyncRequest(changes=changes, last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
        assert len(response.conflicts) == 0
        assert aquarium_id in response.synced_ids
        assert fish_id in response.synced_ids
        assert schedule_id in response.synced_ids

        # Verify entities were actually persisted
        aq = await async_session.get(Aquarium, aquarium_id)
        assert aq is not None
        assert aq.name == "New Tank"

        f = await async_session.get(Fish, fish_id)
        assert f is not None
        assert f.aquarium_id == aquarium_id

        s = await async_session.get(FeedingSchedule, schedule_id)
        assert s is not None
        assert s.aquarium_id == aquarium_id
        assert s.fish_id == fish_id
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# _apply_feeding_log_change: first-write-wins tests
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_log_create_successful_insert(
    async_session: AsyncSession,
):
    """Test _apply_feeding_log_change CREATE: successful insert returns no conflict."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        new_log_id = uuid.uuid4()
        scheduled_for = datetime(2024, 6, 15, 9, 0)

        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=new_log_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "fish_id": str(fish.id),
                "scheduled_for": scheduled_for.isoformat(),
                "action": "fed",
                "device_id": str(uuid.uuid4()),
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        # Verify log was created
        stmt = select(FeedingLog).where(FeedingLog.id == new_log_id)
        result = await async_session.execute(stmt)
        log = result.scalar_one_or_none()

        assert log is not None
        assert log.action == "fed"
        assert log.schedule_id == schedule.id
        assert log.fish_id == fish.id
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_log_create_duplicate_schedule_scheduled_for_returns_conflict(
    async_session: AsyncSession,
):
    """Test _apply_feeding_log_change CREATE: duplicate (schedule_id, scheduled_for) returns conflict (first-write-wins)."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        scheduled_for = datetime(2024, 6, 15, 9, 0)

        # Create existing log
        existing_log = await create_test_feeding_log(
            async_session, schedule.id, fish.id, aquarium.id, user.id,
            scheduled_for=scheduled_for, action="fed",
        )

        # Try to create a second log for the same (schedule_id, scheduled_for)
        new_log_id = uuid.uuid4()
        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=new_log_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "fish_id": str(fish.id),
                "scheduled_for": scheduled_for.isoformat(),
                "action": "skipped",
                "device_id": str(uuid.uuid4()),
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        # Should have conflict - first-write-wins
        assert len(response.conflicts) == 1
        conflict = response.conflicts[0]
        assert conflict.entity_type == "feeding_log"
        assert conflict.entity_id == new_log_id
        assert conflict.resolution == "server_wins"
        assert conflict.server_data["id"] == str(existing_log.id)

        # Verify the new log was NOT created
        stmt = select(FeedingLog).where(FeedingLog.id == new_log_id)
        result = await async_session.execute(stmt)
        assert result.scalar_one_or_none() is None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_log_create_existing_id_returns_conflict(
    async_session: AsyncSession,
):
    """Test _apply_feeding_log_change CREATE: existing log ID returns conflict."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        existing_log = await create_test_feeding_log(
            async_session, schedule.id, fish.id, aquarium.id, user.id,
        )

        # Try to create with same ID
        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=existing_log.id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "fish_id": str(fish.id),
                "scheduled_for": datetime(2024, 7, 1, 9, 0).isoformat(),
                "action": "fed",
                "device_id": str(uuid.uuid4()),
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert len(response.conflicts) == 1
        assert response.conflicts[0].resolution == "server_wins"
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_log_update_ignored(
    async_session: AsyncSession,
):
    """Test that UPDATE operation is ignored for immutable feeding logs."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        existing_log = await create_test_feeding_log(
            async_session, schedule.id, fish.id, aquarium.id, user.id,
            action="fed",
        )

        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=existing_log.id,
            operation="update",
            data={"action": "skipped"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        # No conflicts, update silently ignored
        assert response.conflicts == []

        # Verify log was NOT updated
        await async_session.refresh(existing_log)
        assert existing_log.action == "fed"
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# _apply_schedule_change tests with new fields
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_schedule_change_handles_new_fields(
    async_session: AsyncSession,
):
    """Test _apply_schedule_change handles new fields (fish_id, time, interval_days, etc.)."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)

        new_schedule_id = uuid.uuid4()
        change = ChangeItem(
            entity_type="schedule",
            entity_id=new_schedule_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "fish_id": str(fish.id),
                "time": "14:30",
                "interval_days": 3,
                "anchor_date": "2024-06-01",
                "food_type": "pellets",
                "portion_hint": "small pinch",
                "active": True,
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        stmt = select(FeedingSchedule).where(FeedingSchedule.id == new_schedule_id)
        result = await async_session.execute(stmt)
        schedule = result.scalar_one_or_none()

        assert schedule is not None
        assert schedule.fish_id == fish.id
        assert schedule.time == time(14, 30)
        assert schedule.interval_days == 3
        assert schedule.anchor_date == date(2024, 6, 1)
        assert schedule.food_type == "pellets"
        assert schedule.portion_hint == "small pinch"
        assert schedule.active is True
        assert schedule.created_by_user_id == user.id
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_schedule_update_handles_new_fields(
    async_session: AsyncSession,
):
    """Test schedule update with new fields (time, interval_days, active, etc.)."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        change = ChangeItem(
            entity_type="schedule",
            entity_id=schedule.id,
            operation="update",
            data={
                "time": "18:00",
                "interval_days": 2,
                "active": False,
            },
            client_updated_at=schedule.updated_at + timedelta(hours=1),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        await async_session.refresh(schedule)
        assert schedule.time == time(18, 0)
        assert schedule.interval_days == 2
        assert schedule.active is False
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# _validate_entity_ownership tests for feeding_log
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_validate_entity_ownership_feeding_log(
    async_session: AsyncSession,
):
    """Test _validate_entity_ownership works for feeding_log entity type."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, owner.id)

        existing_log = await create_test_feeding_log(
            async_session, schedule.id, fish.id, aquarium.id, owner.id,
        )

        # Other user tries to update existing log
        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=existing_log.id,
            operation="update",
            data={"action": "skipped"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncAccessDeniedError) as exc_info:
            await process_sync(async_session, other_user.id, request)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# _entity_to_dict tests
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_entity_to_dict_feeding_log(
    async_session: AsyncSession,
):
    """Test _entity_to_dict correctly serializes FeedingLog."""
    from app.services.sync import _entity_to_dict

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        log = await create_test_feeding_log(
            async_session, schedule.id, fish.id, aquarium.id, user.id,
            action="fed",
        )

        result = _entity_to_dict(log)

        assert result["id"] == str(log.id)
        assert result["schedule_id"] == str(schedule.id)
        assert result["fish_id"] == str(fish.id)
        assert result["aquarium_id"] == str(aquarium.id)
        assert result["action"] == "fed"
        assert result["acted_by_user_id"] == str(user.id)
        assert result["device_id"] == str(log.device_id)
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_entity_to_dict_feeding_schedule(
    async_session: AsyncSession,
):
    """Test _entity_to_dict correctly serializes updated FeedingSchedule."""
    from app.services.sync import _entity_to_dict

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(
            async_session, aquarium.id, fish.id, user.id,
            schedule_time=time(14, 30),
        )

        result = _entity_to_dict(schedule)

        assert result["id"] == str(schedule.id)
        assert result["aquarium_id"] == str(aquarium.id)
        assert result["fish_id"] == str(fish.id)
        assert result["time"] == "14:30"
        assert result["interval_days"] == 1
        assert result["active"] is True
        assert result["created_by_user_id"] == str(user.id)
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# get_server_state tests
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_returns_feeding_logs(
    async_session: AsyncSession,
):
    """Test get_server_state returns feeding_logs instead of events."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)
        log = await create_test_feeding_log(
            async_session, schedule.id, fish.id, aquarium.id, user.id,
        )

        server_state = await get_server_state(async_session, user.id, since=None)

        assert len(server_state.feeding_logs) >= 1
        log_ids = {entry["id"] for entry in server_state.feeding_logs}
        assert str(log.id) in log_ids
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_initial_sync_returns_all_active_records(
    async_session: AsyncSession,
):
    """Test that initial sync (since=None) returns all active records."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)
        log = await create_test_feeding_log(
            async_session, schedule.id, fish.id, aquarium.id, user.id,
        )

        server_state = await get_server_state(async_session, user.id, since=None)

        assert len(server_state.aquariums) == 1
        assert len(server_state.fish) == 1
        assert len(server_state.feeding_logs) >= 1

        assert server_state.aquariums[0]["id"] == str(aquarium.id)
        assert server_state.fish[0]["id"] == str(fish.id)

        assert server_state.deleted.aquariums == []
        assert server_state.deleted.fish == []
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_delta_sync_returns_only_updated_records(
    async_session: AsyncSession,
):
    """Test that delta sync returns only records updated after since timestamp."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)

        since_time = fish.updated_at + timedelta(microseconds=1)

        new_fish = await create_test_fish(async_session, aquarium.id)

        server_state = await get_server_state(async_session, user.id, since=since_time)

        assert len(server_state.fish) >= 1
        fish_ids = {f["id"] for f in server_state.fish}
        assert str(new_fish.id) in fish_ids

        # Aquarium was created before since_time
        assert len(server_state.aquariums) == 0
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_delta_sync_includes_deleted_entities(
    async_session: AsyncSession,
):
    """Test that delta sync includes soft-deleted entities in deleted list."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        fish_id = fish.id

        since_time = datetime.now(UTC)

        fish.deleted_at = datetime.now(UTC)
        await async_session.commit()

        server_state = await get_server_state(async_session, user.id, since=since_time)

        assert len(server_state.fish) == 0
        assert len(server_state.deleted.fish) == 1
        assert server_state.deleted.fish[0] == fish_id
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_no_aquariums_returns_empty(
    async_session: AsyncSession,
):
    """Test that user with no aquariums gets empty server state."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)

        server_state = await get_server_state(async_session, user.id, since=None)

        assert server_state.aquariums == []
        assert server_state.fish == []
        assert server_state.feeding_logs == []
        assert server_state.deleted.aquariums == []
        assert server_state.deleted.fish == []
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Full sync flow tests
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_full_sync_flow_feeding_log(
    async_session: AsyncSession,
):
    """Test full sync flow: client sends feeding_log changes, server applies and returns state."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        schedule = await create_test_schedule(async_session, aquarium.id, fish.id, user.id)

        log_id = uuid.uuid4()
        scheduled_for = datetime(2024, 6, 15, 9, 0)

        change = ChangeItem(
            entity_type="feeding_log",
            entity_id=log_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "fish_id": str(fish.id),
                "scheduled_for": scheduled_for.isoformat(),
                "action": "fed",
                "acted_at": datetime.now(UTC).isoformat(),
                "device_id": str(uuid.uuid4()),
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []
        assert response.sync_token is not None

        # Verify feeding_log is in server state
        log_ids = {entry["id"] for entry in response.server_state.feeding_logs}
        assert str(log_id) in log_ids
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Batch and conflict tests
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_create_aquarium_new_entity(
    async_session: AsyncSession,
):
    """Test that CREATE creates new aquarium when entity doesn't exist."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        new_aquarium_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="aquarium",
            entity_id=new_aquarium_id,
            operation="create",
            data={"name": "Test Aquarium"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        stmt = select(Aquarium).where(Aquarium.id == new_aquarium_id)
        result = await async_session.execute(stmt)
        aquarium = result.scalar_one_or_none()

        assert aquarium is not None
        assert aquarium.name == "Test Aquarium"
        assert aquarium.owner_id == user.id
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_update_fish_client_wins(
    async_session: AsyncSession,
):
    """Test that UPDATE with newer client timestamp updates the entity."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)

        client_time = fish.updated_at + timedelta(hours=1)

        change = ChangeItem(
            entity_type="fish",
            entity_id=fish.id,
            operation="update",
            data={"quantity": 10},
            client_updated_at=client_time,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        await async_session.refresh(fish)
        assert fish.quantity == 10
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_update_fish_server_wins_conflict(
    async_session: AsyncSession,
):
    """Test that UPDATE with older client timestamp returns conflict."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        original_quantity = fish.quantity

        client_time = fish.updated_at - timedelta(hours=1)

        change = ChangeItem(
            entity_type="fish",
            entity_id=fish.id,
            operation="update",
            data={"quantity": 100},
            client_updated_at=client_time,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert len(response.conflicts) == 1
        assert response.conflicts[0].resolution == "server_wins"

        await async_session.refresh(fish)
        assert fish.quantity == original_quantity
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_delete_fish_client_wins(
    async_session: AsyncSession,
):
    """Test that DELETE with newer client timestamp soft deletes the entity."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)

        client_time = fish.updated_at + timedelta(hours=1)

        change = ChangeItem(
            entity_type="fish",
            entity_id=fish.id,
            operation="delete",
            data={},
            client_updated_at=client_time,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        await async_session.refresh(fish)
        assert fish.deleted_at is not None
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# resolve_conflict unit tests
# ============================================================================


def test_resolve_conflict_client_wins_when_newer():
    """Test that client wins when client timestamp is newer."""
    from app.services.sync import resolve_conflict

    server_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    client_time = datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC)

    result = resolve_conflict(server_time, client_time)

    assert result == "client"


def test_resolve_conflict_server_wins_when_newer():
    """Test that server wins when server timestamp is newer."""
    from app.services.sync import resolve_conflict

    server_time = datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC)
    client_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = resolve_conflict(server_time, client_time)

    assert result == "server"


def test_resolve_conflict_server_wins_on_tie():
    """Test that server wins when timestamps are equal (determinism)."""
    from app.services.sync import resolve_conflict

    same_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = resolve_conflict(same_time, same_time)

    assert result == "server"


# ============================================================================
# Verify _detect_concurrent_feeding is removed
# ============================================================================


def test_detect_concurrent_feeding_removed():
    """Verify _detect_concurrent_feeding function is removed."""
    import app.services.sync as sync_module

    assert not hasattr(sync_module, "_detect_concurrent_feeding")
    assert not hasattr(sync_module, "CONCURRENT_FEEDING_WINDOW")


# ============================================================================
# _ensure_schedules_for_user tests (per-fish schedule generation)
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
class TestEnsureSchedulesPerFish:
    """Tests for _ensure_schedules_for_user — per-fish schedule generation."""

    async def test_creates_schedules_for_fish_without_schedules(
        self, async_session: AsyncSession
    ):
        """Fish without schedules should get schedules created."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            fish = await create_test_fish(async_session, aquarium.id)

            # No schedules initially
            stmt = select(FeedingSchedule).where(FeedingSchedule.fish_id == fish.id)
            result = await async_session.execute(stmt)
            assert len(result.scalars().all()) == 0

            # Run ensure schedules
            await _ensure_schedules_for_user(async_session, user.id)

            # Now fish should have schedules
            result = await async_session.execute(stmt)
            schedules = result.scalars().all()
            assert len(schedules) == 2  # test-guppy has feeding_frequency=2
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_new_fish_gets_schedules_even_if_aquarium_has_existing(
        self, async_session: AsyncSession
    ):
        """New fish in aquarium with existing schedules should still get its own schedules."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)

            # Create first fish with schedules
            fish1 = await create_test_fish(async_session, aquarium.id, "test-guppy")
            await create_test_schedule(async_session, aquarium.id, fish1.id, user.id, time(9, 0))
            await create_test_schedule(async_session, aquarium.id, fish1.id, user.id, time(18, 0))

            # Create second fish WITHOUT schedules
            species2 = await ensure_test_species(async_session, "test-betta")
            fish2 = Fish(
                aquarium_id=aquarium.id,
                species_id=species2.id,
                quantity=1,
            )
            async_session.add(fish2)
            await async_session.commit()
            await async_session.refresh(fish2)

            # Verify fish2 has no schedules
            stmt = select(FeedingSchedule).where(FeedingSchedule.fish_id == fish2.id)
            result = await async_session.execute(stmt)
            assert len(result.scalars().all()) == 0

            # Run ensure schedules — this is the key test!
            # Old per-aquarium logic would skip because aquarium already has schedules.
            # New per-fish logic should create schedules for fish2.
            await _ensure_schedules_for_user(async_session, user.id)

            # fish2 should now have schedules
            result = await async_session.execute(stmt)
            schedules = result.scalars().all()
            assert len(schedules) == 2  # test-betta gets schedules based on species frequency

            # fish1 should still have its original schedules (not duplicated)
            stmt1 = select(FeedingSchedule).where(FeedingSchedule.fish_id == fish1.id)
            result1 = await async_session.execute(stmt1)
            assert len(result1.scalars().all()) == 2
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_skips_fish_with_existing_schedules(
        self, async_session: AsyncSession
    ):
        """Fish that already has schedules should not get duplicates."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            fish = await create_test_fish(async_session, aquarium.id)

            # Create schedules manually
            await create_test_schedule(async_session, aquarium.id, fish.id, user.id, time(9, 0))
            await create_test_schedule(async_session, aquarium.id, fish.id, user.id, time(18, 0))

            # Run ensure schedules
            await _ensure_schedules_for_user(async_session, user.id)

            # Should still have only 2 schedules (no duplicates)
            stmt = select(FeedingSchedule).where(FeedingSchedule.fish_id == fish.id)
            result = await async_session.execute(stmt)
            assert len(result.scalars().all()) == 2
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_no_fish_does_nothing(self, async_session: AsyncSession):
        """Aquarium with no fish should not cause errors."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)

            # Should not raise
            await _ensure_schedules_for_user(async_session, user.id)

            # No schedules created
            stmt = select(FeedingSchedule).where(FeedingSchedule.aquarium_id == aquarium.id)
            result = await async_session.execute(stmt)
            assert len(result.scalars().all()) == 0
        finally:
            await cleanup_sync_test_data(async_session)


# ============================================================================
# Fish soft-delete deactivates schedules tests
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
class TestFishDeleteDeactivatesSchedules:
    """Tests for fish deletion cascading to schedule deactivation."""

    async def test_soft_delete_fish_deactivates_its_schedules(
        self, async_session: AsyncSession
    ):
        """When fish is soft-deleted, its schedules should be deactivated."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            fish = await create_test_fish(async_session, aquarium.id)

            # Create schedules for fish
            schedule1 = await create_test_schedule(
                async_session, aquarium.id, fish.id, user.id, time(9, 0)
            )
            schedule2 = await create_test_schedule(
                async_session, aquarium.id, fish.id, user.id, time(18, 0)
            )

            # Verify schedules are active
            assert schedule1.active is True
            assert schedule2.active is True

            # Soft-delete fish via sync
            delete_change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="delete",
                data={},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=1),
            )
            request = SyncRequest(changes=[delete_change])
            await process_sync(async_session, user.id, request)

            # Verify fish is soft-deleted
            await async_session.refresh(fish)
            assert fish.deleted_at is not None

            # Verify schedules are deactivated
            await async_session.refresh(schedule1)
            await async_session.refresh(schedule2)
            assert schedule1.active is False
            assert schedule2.active is False
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_delta_sync_returns_deactivated_schedules_as_deleted(
        self, async_session: AsyncSession
    ):
        """Delta sync should return deactivated schedules in deleted.schedules."""
        from app.services.sync import get_server_state

        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            fish = await create_test_fish(async_session, aquarium.id)

            # Create schedule
            schedule = await create_test_schedule(
                async_session, aquarium.id, fish.id, user.id, time(9, 0)
            )

            # Record time before deactivation
            before_deactivation = datetime.now(UTC)

            # Deactivate schedule (simulating fish deletion)
            schedule.active = False
            schedule.updated_at = datetime.now(UTC) + timedelta(seconds=1)
            await async_session.commit()

            # Delta sync should return schedule in deleted.schedules
            state = await get_server_state(async_session, user.id, before_deactivation)

            assert schedule.id in state.deleted.schedules
            assert len(state.schedules) == 0  # No active schedules returned
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_initial_sync_excludes_inactive_schedules(
        self, async_session: AsyncSession
    ):
        """Initial sync should not return inactive schedules."""
        from app.services.sync import get_server_state

        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            fish = await create_test_fish(async_session, aquarium.id)

            # Create active schedule
            active_schedule = await create_test_schedule(
                async_session, aquarium.id, fish.id, user.id, time(9, 0)
            )

            # Create inactive schedule
            inactive_schedule = FeedingSchedule(
                aquarium_id=aquarium.id,
                fish_id=fish.id,
                time=time(18, 0),
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
                active=False,  # Inactive!
                created_by_user_id=user.id,
            )
            async_session.add(inactive_schedule)
            await async_session.commit()
            await async_session.refresh(inactive_schedule)

            # Initial sync (since=None)
            state = await get_server_state(async_session, user.id, since=None)

            # Should only return active schedule
            schedule_ids = [s["id"] for s in state.schedules]
            assert str(active_schedule.id) in schedule_ids
            assert str(inactive_schedule.id) not in schedule_ids
        finally:
            await cleanup_sync_test_data(async_session)


# ============================================================================
# Server does NOT auto-generate schedules (offline-first architecture)
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
class TestServerDoesNotAutoGenerateSchedules:
    """Tests verifying server is passive and does not generate schedules.

    Per offline-first architecture:
    - Client creates schedules locally
    - Client sends schedules via sync
    - Server only stores what client sends
    - Server NEVER generates schedules automatically
    """

    async def test_sync_does_not_create_schedules_for_new_fish(
        self, async_session: AsyncSession
    ):
        """When client creates fish via sync, server should NOT auto-generate schedules."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            await ensure_test_species(async_session, "test-guppy")

            # Create fish via sync (like mobile client would)
            fish_id = uuid.uuid4()
            fish_change = ChangeItem(
                entity_type="fish",
                entity_id=fish_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium.id),
                    "species_id": "test-guppy",
                    "quantity": 1,
                },
                client_updated_at=datetime.now(UTC),
            )
            request = SyncRequest(changes=[fish_change])
            response = await process_sync(async_session, user.id, request)

            # Verify fish was created
            stmt = select(Fish).where(Fish.id == fish_id)
            result = await async_session.execute(stmt)
            fish = result.scalar_one_or_none()
            assert fish is not None, "Fish should be created"

            # Verify NO schedules were auto-generated
            schedule_stmt = select(FeedingSchedule).where(FeedingSchedule.fish_id == fish_id)
            result = await async_session.execute(schedule_stmt)
            schedules = result.scalars().all()
            assert len(schedules) == 0, "Server should NOT auto-generate schedules"

            # Verify response also has no schedules for this fish
            response_schedule_fish_ids = [s.get("fish_id") for s in response.server_state.schedules]
            assert str(fish_id) not in response_schedule_fish_ids
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_sync_with_fish_does_not_trigger_schedule_generation(
        self, async_session: AsyncSession
    ):
        """Multiple syncs with fish changes should never trigger schedule generation."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            await ensure_test_species(async_session, "test-guppy")

            # First sync: create fish
            fish_id = uuid.uuid4()
            create_change = ChangeItem(
                entity_type="fish",
                entity_id=fish_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium.id),
                    "species_id": "test-guppy",
                    "quantity": 1,
                },
                client_updated_at=datetime.now(UTC),
            )
            await process_sync(async_session, user.id, SyncRequest(changes=[create_change]))

            # Second sync: update fish
            update_change = ChangeItem(
                entity_type="fish",
                entity_id=fish_id,
                operation="update",
                data={"quantity": 5},
                client_updated_at=datetime.now(UTC) + timedelta(seconds=1),
            )
            await process_sync(async_session, user.id, SyncRequest(changes=[update_change]))

            # Third sync: empty (just fetching state)
            await process_sync(async_session, user.id, SyncRequest(changes=[]))

            # After all syncs, still NO schedules should exist
            schedule_stmt = select(FeedingSchedule).where(FeedingSchedule.fish_id == fish_id)
            result = await async_session.execute(schedule_stmt)
            schedules = result.scalars().all()
            assert len(schedules) == 0, "Server should NEVER auto-generate schedules"
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_client_created_schedules_are_stored(
        self, async_session: AsyncSession
    ):
        """Schedules sent by client via sync should be stored correctly."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            fish = await create_test_fish(async_session, aquarium.id)

            # Client creates schedule and sends via sync
            schedule_id = uuid.uuid4()
            schedule_change = ChangeItem(
                entity_type="schedule",
                entity_id=schedule_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium.id),
                    "fish_id": str(fish.id),
                    "time": "09:00",
                    "interval_days": 1,
                    "anchor_date": date.today().isoformat(),
                    "food_type": "flakes",
                    "active": True,
                },
                client_updated_at=datetime.now(UTC),
            )
            request = SyncRequest(changes=[schedule_change])
            await process_sync(async_session, user.id, request)

            # Verify schedule was stored
            stmt = select(FeedingSchedule).where(FeedingSchedule.id == schedule_id)
            result = await async_session.execute(stmt)
            schedule = result.scalar_one_or_none()
            assert schedule is not None, "Client-created schedule should be stored"
            assert schedule.fish_id == fish.id
            assert schedule.time == time(9, 0)
        finally:
            await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestSyncedIds:
    """Tests verifying synced_ids in sync response.

    The server must return entity IDs that were successfully accepted,
    so the client can mark them as synced and stop resending.
    """

    async def test_synced_ids_returned_for_accepted_changes(
        self, async_session: AsyncSession
    ):
        """All accepted changes (no conflict) should appear in synced_ids."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            await ensure_test_species(async_session, "test-guppy")

            fish_id = uuid.uuid4()
            schedule_id = uuid.uuid4()

            changes = [
                ChangeItem(
                    entity_type="fish",
                    entity_id=fish_id,
                    operation="create",
                    data={
                        "aquarium_id": str(aquarium.id),
                        "species_id": "test-guppy",
                        "quantity": 2,
                    },
                    client_updated_at=datetime.now(UTC),
                ),
                ChangeItem(
                    entity_type="schedule",
                    entity_id=schedule_id,
                    operation="create",
                    data={
                        "aquarium_id": str(aquarium.id),
                        "fish_id": str(fish_id),
                        "time": "09:00",
                    },
                    client_updated_at=datetime.now(UTC),
                ),
            ]
            response = await process_sync(
                async_session, user.id, SyncRequest(changes=changes)
            )

            assert len(response.synced_ids) == 2
            assert fish_id in response.synced_ids
            assert schedule_id in response.synced_ids
            assert len(response.conflicts) == 0
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_synced_ids_empty_when_no_changes(
        self, async_session: AsyncSession
    ):
        """Empty changes list should return empty synced_ids."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            response = await process_sync(
                async_session, user.id, SyncRequest(changes=[])
            )

            assert response.synced_ids == []
        finally:
            await cleanup_sync_test_data(async_session)

    async def test_synced_ids_excludes_conflicts(
        self, async_session: AsyncSession
    ):
        """Conflicted changes should NOT appear in synced_ids."""
        await cleanup_sync_test_data(async_session)
        try:
            user = await create_test_user(async_session)
            aquarium = await create_test_aquarium(async_session, user.id)
            fish = await create_test_fish(async_session, aquarium.id)

            # Send update with old timestamp so server wins (= conflict)
            conflicting_change = ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"quantity": 99},
                client_updated_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
            # Send a new schedule that will succeed
            schedule_id = uuid.uuid4()
            accepted_change = ChangeItem(
                entity_type="schedule",
                entity_id=schedule_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium.id),
                    "fish_id": str(fish.id),
                    "time": "10:00",
                },
                client_updated_at=datetime.now(UTC),
            )
            response = await process_sync(
                async_session,
                user.id,
                SyncRequest(changes=[conflicting_change, accepted_change]),
            )

            # Only the accepted change should be in synced_ids
            assert schedule_id in response.synced_ids
            assert fish.id not in response.synced_ids
            assert len(response.conflicts) == 1
            assert response.conflicts[0].entity_id == fish.id
        finally:
            await cleanup_sync_test_data(async_session)
