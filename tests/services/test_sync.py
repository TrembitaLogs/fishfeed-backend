"""Integration tests for sync service."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingEvent, FeedingSchedule
from app.models.fish import Fish
from app.models.species import Species
from app.models.user import User
from app.schemas.sync import ChangeItem, SyncRequest
from app.services.sync import (
    SyncAccessDeniedError,
    SyncValidationError,
    process_sync,
)


async def cleanup_sync_test_data(session: AsyncSession) -> None:
    """Helper to cleanup sync test data."""
    await session.execute(text("DELETE FROM feeding_events"))
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
    from sqlalchemy import select

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


async def create_test_feeding_event(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
    scheduled_at: datetime | None = None,
) -> FeedingEvent:
    """Helper to create a test feeding event."""
    event = FeedingEvent(
        aquarium_id=aquarium_id,
        scheduled_at=scheduled_at or datetime.now(UTC),
        status="pending",
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


# process_sync tests - empty changes


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


# Entity ownership validation tests - aquarium


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

        # Should not raise - create is allowed
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


# Entity ownership validation tests - fish


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
            data={"species_id": "test-guppy"},  # Missing aquarium_id
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncValidationError) as exc_info:
            await process_sync(async_session, user.id, request)

        assert "aquarium_id" in exc_info.value.message
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_update_fish_in_owned_aquarium(
    async_session: AsyncSession,
):
    """Test that update fish is allowed for fish in owned aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)

        change = ChangeItem(
            entity_type="fish",
            entity_id=fish.id,
            operation="update",
            data={"quantity": 5},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_denies_update_fish_in_other_user_aquarium(
    async_session: AsyncSession,
):
    """Test that update fish is denied for fish in other user's aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        fish = await create_test_fish(async_session, aquarium.id)

        change = ChangeItem(
            entity_type="fish",
            entity_id=fish.id,
            operation="update",
            data={"quantity": 100},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncAccessDeniedError) as exc_info:
            await process_sync(async_session, other_user.id, request)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_sync_test_data(async_session)


# Entity ownership validation tests - events


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_create_event_in_owned_aquarium(
    async_session: AsyncSession,
):
    """Test that create event is allowed in owned aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        change = ChangeItem(
            entity_type="event",
            entity_id=uuid.uuid4(),
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "scheduled_at": datetime.now(UTC).isoformat(),
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_denies_create_event_in_other_user_aquarium(
    async_session: AsyncSession,
):
    """Test that create event is denied in other user's aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)

        change = ChangeItem(
            entity_type="event",
            entity_id=uuid.uuid4(),
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "scheduled_at": datetime.now(UTC).isoformat(),
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
async def test_process_sync_denies_create_event_missing_aquarium_id(
    async_session: AsyncSession,
):
    """Test that create event without aquarium_id raises validation error."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)

        change = ChangeItem(
            entity_type="event",
            entity_id=uuid.uuid4(),
            operation="create",
            data={"scheduled_at": datetime.now(UTC).isoformat()},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncValidationError) as exc_info:
            await process_sync(async_session, user.id, request)

        assert "aquarium_id" in exc_info.value.message
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_update_event_in_owned_aquarium(
    async_session: AsyncSession,
):
    """Test that update event is allowed for event in owned aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        event = await create_test_feeding_event(async_session, aquarium.id)

        change = ChangeItem(
            entity_type="event",
            entity_id=event.id,
            operation="update",
            data={"status": "completed"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_denies_update_event_in_other_user_aquarium(
    async_session: AsyncSession,
):
    """Test that update event is denied for event in other user's aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        event = await create_test_feeding_event(async_session, aquarium.id)

        change = ChangeItem(
            entity_type="event",
            entity_id=event.id,
            operation="update",
            data={"status": "completed"},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        with pytest.raises(SyncAccessDeniedError) as exc_info:
            await process_sync(async_session, other_user.id, request)

        assert exc_info.value.status_code == 403
    finally:
        await cleanup_sync_test_data(async_session)


# Member access tests


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_member_to_update_shared_aquarium_fish(
    async_session: AsyncSession,
):
    """Test that aquarium members can update fish in shared aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        fish = await create_test_fish(async_session, aquarium.id)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        change = ChangeItem(
            entity_type="fish",
            entity_id=fish.id,
            operation="update",
            data={"quantity": 3},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        # Member should be allowed
        response = await process_sync(async_session, member_user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


# Multiple changes tests


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_validates_all_changes(
    async_session: AsyncSession,
):
    """Test that sync validates all changes before processing."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)

        # First change is valid (create new aquarium)
        # Second change is invalid (access other user's aquarium)
        changes = [
            ChangeItem(
                entity_type="aquarium",
                entity_id=uuid.uuid4(),
                operation="create",
                data={"name": "My New Aquarium"},
                client_updated_at=datetime.now(UTC),
            ),
            ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"name": "Hacked"},
                client_updated_at=datetime.now(UTC),
            ),
        ]
        request = SyncRequest(changes=changes, last_sync_at=None)

        # Should fail on the second change
        with pytest.raises(SyncAccessDeniedError):
            await process_sync(async_session, other_user.id, request)
    finally:
        await cleanup_sync_test_data(async_session)


# Non-existent entity tests


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_update_nonexistent_fish(
    async_session: AsyncSession,
):
    """Test that update for non-existent fish passes validation (handled by apply_changes)."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        nonexistent_fish_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="fish",
            entity_id=nonexistent_fish_id,
            operation="update",
            data={"quantity": 5},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        # Should not raise - non-existent entities pass validation
        # (will be handled by apply_changes in task 6.3)
        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_allows_delete_nonexistent_event(
    async_session: AsyncSession,
):
    """Test that delete for non-existent event passes validation."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        nonexistent_event_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="event",
            entity_id=nonexistent_event_id,
            operation="delete",
            data={},
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.sync_token is not None
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Task 6.3: apply_changes with last-write-wins conflict resolution tests
# ============================================================================


# resolve_conflict unit tests


def test_resolve_conflict_client_wins_when_newer():
    """Test that client wins when client timestamp is newer."""
    from app.services.sync import resolve_conflict

    server_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    client_time = datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC)  # 1 second newer

    result = resolve_conflict(server_time, client_time)

    assert result == "client"


def test_resolve_conflict_server_wins_when_newer():
    """Test that server wins when server timestamp is newer."""
    from app.services.sync import resolve_conflict

    server_time = datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC)  # 1 second newer
    client_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = resolve_conflict(server_time, client_time)

    assert result == "server"


def test_resolve_conflict_server_wins_on_tie():
    """Test that server wins when timestamps are equal (determinism)."""
    from app.services.sync import resolve_conflict

    same_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    result = resolve_conflict(same_time, same_time)

    assert result == "server"


# apply_changes CREATE tests


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

        # Verify no conflicts
        assert response.conflicts == []

        # Verify aquarium was created
        stmt = select(Aquarium).where(Aquarium.id == new_aquarium_id)
        result = await async_session.execute(stmt)
        aquarium = result.scalar_one_or_none()

        assert aquarium is not None
        assert aquarium.name == "Test Aquarium"
        assert aquarium.owner_id == user.id
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_create_fish_new_entity(
    async_session: AsyncSession,
):
    """Test that CREATE creates new fish when entity doesn't exist."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        await ensure_test_species(async_session)
        new_fish_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="fish",
            entity_id=new_fish_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "species_id": "test-guppy",
                "quantity": 5,
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        # Verify fish was created
        stmt = select(Fish).where(Fish.id == new_fish_id)
        result = await async_session.execute(stmt)
        fish = result.scalar_one_or_none()

        assert fish is not None
        assert fish.quantity == 5
        assert fish.species_id == "test-guppy"
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_create_event_new_entity(
    async_session: AsyncSession,
):
    """Test that CREATE creates new feeding event when entity doesn't exist."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        new_event_id = uuid.uuid4()
        scheduled_at = datetime.now(UTC)

        change = ChangeItem(
            entity_type="event",
            entity_id=new_event_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "scheduled_at": scheduled_at.isoformat(),
                "status": "pending",
            },
            client_updated_at=datetime.now(UTC),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        # Verify event was created
        stmt = select(FeedingEvent).where(FeedingEvent.id == new_event_id)
        result = await async_session.execute(stmt)
        event = result.scalar_one_or_none()

        assert event is not None
        assert event.status == "pending"
    finally:
        await cleanup_sync_test_data(async_session)


# apply_changes UPDATE tests - client wins


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

        # Client timestamp is 1 hour newer than server
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

        # No conflicts - client wins
        assert response.conflicts == []

        # Verify fish was updated
        await async_session.refresh(fish)
        assert fish.quantity == 10
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_update_aquarium_client_wins(
    async_session: AsyncSession,
):
    """Test that UPDATE with newer client timestamp updates aquarium."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Client timestamp is 1 hour newer than server
        client_time = aquarium.updated_at + timedelta(hours=1)

        change = ChangeItem(
            entity_type="aquarium",
            entity_id=aquarium.id,
            operation="update",
            data={"name": "Updated Name"},
            client_updated_at=client_time,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        await async_session.refresh(aquarium)
        assert aquarium.name == "Updated Name"
    finally:
        await cleanup_sync_test_data(async_session)


# apply_changes UPDATE tests - server wins (conflict)


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

        # Client timestamp is 1 hour OLDER than server
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

        # Should have 1 conflict
        assert len(response.conflicts) == 1

        conflict = response.conflicts[0]
        assert conflict.entity_type == "fish"
        assert conflict.entity_id == fish.id
        assert conflict.resolution == "server_wins"
        assert conflict.client_data == {"quantity": 100}

        # Verify fish was NOT updated
        await async_session.refresh(fish)
        assert fish.quantity == original_quantity
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_update_event_server_wins_conflict(
    async_session: AsyncSession,
):
    """Test that UPDATE event with older timestamp returns conflict."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        event = await create_test_feeding_event(async_session, aquarium.id)
        original_status = event.status

        # Client timestamp is older
        client_time = event.updated_at - timedelta(hours=1)

        change = ChangeItem(
            entity_type="event",
            entity_id=event.id,
            operation="update",
            data={"status": "completed"},
            client_updated_at=client_time,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert len(response.conflicts) == 1
        assert response.conflicts[0].resolution == "server_wins"

        # Verify event was NOT updated
        await async_session.refresh(event)
        assert event.status == original_status
    finally:
        await cleanup_sync_test_data(async_session)


# apply_changes DELETE tests


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

        # Client timestamp is newer
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

        # Verify fish was soft deleted
        await async_session.refresh(fish)
        assert fish.deleted_at is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_delete_event_client_wins(
    async_session: AsyncSession,
):
    """Test that DELETE event with newer timestamp hard deletes (no SoftDeleteMixin)."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        event = await create_test_feeding_event(async_session, aquarium.id)
        event_id = event.id

        # Client timestamp is newer
        client_time = event.updated_at + timedelta(hours=1)

        change = ChangeItem(
            entity_type="event",
            entity_id=event_id,
            operation="delete",
            data={},
            client_updated_at=client_time,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        # Verify event was hard deleted
        stmt = select(FeedingEvent).where(FeedingEvent.id == event_id)
        result = await async_session.execute(stmt)
        deleted_event = result.scalar_one_or_none()
        assert deleted_event is None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_delete_aquarium_server_wins_conflict(
    async_session: AsyncSession,
):
    """Test that DELETE with older client timestamp returns conflict."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Client timestamp is older
        client_time = aquarium.updated_at - timedelta(hours=1)

        change = ChangeItem(
            entity_type="aquarium",
            entity_id=aquarium.id,
            operation="delete",
            data={},
            client_updated_at=client_time,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert len(response.conflicts) == 1
        assert response.conflicts[0].resolution == "server_wins"

        # Verify aquarium was NOT deleted
        await async_session.refresh(aquarium)
        assert aquarium.deleted_at is None
    finally:
        await cleanup_sync_test_data(async_session)


# CREATE existing entity tests (treated as update)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_create_existing_fish_client_wins(
    async_session: AsyncSession,
):
    """Test that CREATE for existing entity is treated as update."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)

        # Client timestamp is newer
        client_time = fish.updated_at + timedelta(hours=1)

        change = ChangeItem(
            entity_type="fish",
            entity_id=fish.id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "species_id": "test-guppy",
                "quantity": 15,
            },
            client_updated_at=client_time,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        # No conflicts - client wins, treated as update
        assert response.conflicts == []

        await async_session.refresh(fish)
        assert fish.quantity == 15
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_create_existing_fish_server_wins_conflict(
    async_session: AsyncSession,
):
    """Test that CREATE for existing entity with older timestamp returns conflict."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        original_quantity = fish.quantity

        # Client timestamp is older
        client_time = fish.updated_at - timedelta(hours=1)

        change = ChangeItem(
            entity_type="fish",
            entity_id=fish.id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "species_id": "test-guppy",
                "quantity": 15,
            },
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


# Batch processing tests


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_batch_multiple_entity_types(
    async_session: AsyncSession,
):
    """Test that changes across multiple entity types are processed correctly."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        await ensure_test_species(async_session)

        new_fish_id = uuid.uuid4()
        new_event_id = uuid.uuid4()
        now = datetime.now(UTC)

        changes = [
            ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"name": "Batch Updated Aquarium"},
                client_updated_at=aquarium.updated_at + timedelta(hours=1),
            ),
            ChangeItem(
                entity_type="fish",
                entity_id=new_fish_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium.id),
                    "species_id": "test-guppy",
                    "quantity": 3,
                },
                client_updated_at=now,
            ),
            ChangeItem(
                entity_type="event",
                entity_id=new_event_id,
                operation="create",
                data={
                    "aquarium_id": str(aquarium.id),
                    "scheduled_at": now.isoformat(),
                },
                client_updated_at=now,
            ),
        ]
        request = SyncRequest(changes=changes, last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        assert response.conflicts == []

        # Verify all changes applied
        await async_session.refresh(aquarium)
        assert aquarium.name == "Batch Updated Aquarium"

        stmt = select(Fish).where(Fish.id == new_fish_id)
        result = await async_session.execute(stmt)
        fish = result.scalar_one_or_none()
        assert fish is not None
        assert fish.quantity == 3

        stmt = select(FeedingEvent).where(FeedingEvent.id == new_event_id)
        result = await async_session.execute(stmt)
        event = result.scalar_one_or_none()
        assert event is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_changes_batch_with_mixed_conflicts(
    async_session: AsyncSession,
):
    """Test batch processing with some conflicts and some successes."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)

        # First change: update fish with newer timestamp (should succeed)
        # Second change: update aquarium with older timestamp (should conflict)
        changes = [
            ChangeItem(
                entity_type="fish",
                entity_id=fish.id,
                operation="update",
                data={"quantity": 20},
                client_updated_at=fish.updated_at + timedelta(hours=1),
            ),
            ChangeItem(
                entity_type="aquarium",
                entity_id=aquarium.id,
                operation="update",
                data={"name": "Should Not Update"},
                client_updated_at=aquarium.updated_at - timedelta(hours=1),
            ),
        ]
        request = SyncRequest(changes=changes, last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        # Should have 1 conflict (aquarium)
        assert len(response.conflicts) == 1
        assert response.conflicts[0].entity_type == "aquarium"

        # Fish should be updated
        await async_session.refresh(fish)
        assert fish.quantity == 20

        # Aquarium should NOT be updated
        await async_session.refresh(aquarium)
        assert aquarium.name == "Test Aquarium"  # Original name
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Task 6.4: get_server_state with delta sync tests
# ============================================================================


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
        event = await create_test_feeding_event(async_session, aquarium.id)

        # Initial sync - no since parameter
        server_state = await get_server_state(async_session, user.id, since=None)

        # Should return all entities
        assert len(server_state.aquariums) == 1
        assert len(server_state.fish) == 1
        assert len(server_state.events) == 1

        # Verify aquarium data
        assert server_state.aquariums[0]["id"] == str(aquarium.id)
        assert server_state.aquariums[0]["name"] == aquarium.name

        # Verify fish data
        assert server_state.fish[0]["id"] == str(fish.id)

        # Verify event data
        assert server_state.events[0]["id"] == str(event.id)

        # No deleted entities on initial sync
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

        # Use the fish's updated_at as the since_time reference point
        # Add a small offset to ensure new fish is "after" since_time
        since_time = fish.updated_at + timedelta(microseconds=1)

        # Create a new fish after since_time
        new_fish = await create_test_fish(async_session, aquarium.id)

        # Delta sync
        server_state = await get_server_state(async_session, user.id, since=since_time)

        # Should return only the new fish (created after since_time)
        assert len(server_state.fish) >= 1
        fish_ids = {f["id"] for f in server_state.fish}
        assert str(new_fish.id) in fish_ids

        # Aquarium was created before since_time, should not be included
        assert len(server_state.aquariums) == 0

        # No deleted entities
        assert server_state.deleted.aquariums == []
        assert server_state.deleted.fish == []
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

        # Record the time before deletion
        since_time = datetime.now(UTC)

        # Soft delete the fish
        fish.deleted_at = datetime.now(UTC)
        await async_session.commit()

        # Delta sync
        server_state = await get_server_state(async_session, user.id, since=since_time)

        # Fish should be in deleted list, not in active list
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
        assert server_state.events == []
        assert server_state.deleted.aquariums == []
        assert server_state.deleted.fish == []
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_excludes_deleted_aquariums_on_initial_sync(
    async_session: AsyncSession,
):
    """Test that initial sync excludes soft-deleted aquariums."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Soft delete the aquarium
        aquarium.deleted_at = datetime.now(UTC)
        await async_session.commit()

        # Initial sync should not include deleted aquarium
        server_state = await get_server_state(async_session, user.id, since=None)

        assert len(server_state.aquariums) == 0
        # Deleted list is empty on initial sync (no since parameter)
        assert server_state.deleted.aquariums == []
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_excludes_deleted_fish_on_initial_sync(
    async_session: AsyncSession,
):
    """Test that initial sync excludes soft-deleted fish."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)

        # Soft delete the fish
        fish.deleted_at = datetime.now(UTC)
        await async_session.commit()

        # Initial sync should not include deleted fish
        server_state = await get_server_state(async_session, user.id, since=None)

        assert len(server_state.fish) == 0
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_member_access_includes_shared_aquarium(
    async_session: AsyncSession,
):
    """Test that members can see shared aquarium data."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        fish = await create_test_fish(async_session, aquarium.id)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Member should see the shared aquarium data
        server_state = await get_server_state(async_session, member_user.id, since=None)

        assert len(server_state.aquariums) == 1
        assert server_state.aquariums[0]["id"] == str(aquarium.id)
        assert len(server_state.fish) == 1
        assert server_state.fish[0]["id"] == str(fish.id)
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_delta_sync_deleted_aquarium(
    async_session: AsyncSession,
):
    """Test that delta sync includes deleted aquarium in deleted list."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        aquarium_id = aquarium.id

        # Record time before deletion
        since_time = datetime.now(UTC)

        # Soft delete the aquarium
        aquarium.deleted_at = datetime.now(UTC)
        await async_session.commit()

        # Delta sync should include deleted aquarium
        server_state = await get_server_state(async_session, user.id, since=since_time)

        assert len(server_state.aquariums) == 0
        assert len(server_state.deleted.aquariums) == 1
        assert server_state.deleted.aquariums[0] == aquarium_id
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_multiple_aquariums(
    async_session: AsyncSession,
):
    """Test that all user's aquariums are returned."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium1 = await create_test_aquarium(async_session, user.id, "Aquarium 1")
        aquarium2 = await create_test_aquarium(async_session, user.id, "Aquarium 2")

        server_state = await get_server_state(async_session, user.id, since=None)

        assert len(server_state.aquariums) == 2
        aquarium_ids = {aq["id"] for aq in server_state.aquariums}
        assert str(aquarium1.id) in aquarium_ids
        assert str(aquarium2.id) in aquarium_ids
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_state_delta_sync_events(
    async_session: AsyncSession,
):
    """Test that delta sync returns only events updated after since."""
    from app.services.sync import get_server_state

    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        event1 = await create_test_feeding_event(async_session, aquarium.id)

        # Use event1's updated_at as the since_time reference point
        # Add a small offset to ensure new event is "after" since_time
        since_time = event1.updated_at + timedelta(microseconds=1)

        # Create second event after since_time
        event2 = await create_test_feeding_event(async_session, aquarium.id)

        # Delta sync
        server_state = await get_server_state(async_session, user.id, since=since_time)

        # Should return only the new event (created after since_time)
        assert len(server_state.events) >= 1
        event_ids = {e["id"] for e in server_state.events}
        assert str(event2.id) in event_ids
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_process_sync_includes_server_state_with_entities(
    async_session: AsyncSession,
):
    """Test that process_sync returns server_state with user's entities."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        fish = await create_test_fish(async_session, aquarium.id)
        event = await create_test_feeding_event(async_session, aquarium.id)

        request = SyncRequest(changes=[], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        # Verify server_state contains entities
        assert len(response.server_state.aquariums) == 1
        assert len(response.server_state.fish) == 1
        assert len(response.server_state.events) == 1
        assert response.server_state.aquariums[0]["id"] == str(aquarium.id)
        assert response.server_state.fish[0]["id"] == str(fish.id)
        assert response.server_state.events[0]["id"] == str(event.id)
    finally:
        await cleanup_sync_test_data(async_session)


# ============================================================================
# Task 6.5: Concurrent feeding conflict detection tests
# ============================================================================


async def create_test_feeding_schedule(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
) -> FeedingSchedule:
    """Helper to create a test feeding schedule."""
    from app.models.feeding import FeedingSchedule

    schedule = FeedingSchedule(
        aquarium_id=aquarium_id,
        times_per_day=2,
        scheduled_times=["08:00", "18:00"],
        food_type="flakes",
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule


async def create_completed_feeding_event(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
    schedule_id: uuid.UUID,
    completed_at: datetime,
    completed_by: uuid.UUID,
) -> FeedingEvent:
    """Helper to create a completed feeding event."""
    event = FeedingEvent(
        aquarium_id=aquarium_id,
        schedule_id=schedule_id,
        scheduled_at=completed_at,
        status="completed",
        completed_at=completed_at,
        completed_by=completed_by,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_feeding_within_window_returns_conflict(
    async_session: AsyncSession,
):
    """Test that two feeding events within 5min window return concurrent_feeding conflict."""
    await cleanup_sync_test_data(async_session)
    try:
        # Create two users (owner and member)
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        schedule = await create_test_feeding_schedule(async_session, aquarium.id)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Create a completed feeding event by owner (on server)
        now = datetime.now(UTC)
        server_event = await create_completed_feeding_event(
            async_session,
            aquarium.id,
            schedule.id,
            completed_at=now,
            completed_by=owner.id,
        )

        # Member tries to sync a feeding event within 5min window
        client_completed_at = now + timedelta(minutes=2)
        new_event_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="event",
            entity_id=new_event_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "scheduled_at": now.isoformat(),
                "status": "completed",
                "completed_at": client_completed_at.isoformat(),
                "completed_by": str(member_user.id),
            },
            client_updated_at=client_completed_at,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, member_user.id, request)

        # Should have concurrent_feeding conflict
        assert len(response.conflicts) == 1
        conflict = response.conflicts[0]
        assert conflict.entity_type == "event"
        assert conflict.entity_id == new_event_id
        assert conflict.resolution == "concurrent_feeding"

        # Server data should contain the existing event
        assert conflict.server_data["id"] == str(server_event.id)
        assert conflict.server_data["completed_by"] == str(owner.id)

        # Client data should contain the client's event data
        assert conflict.client_data["completed_by"] == str(member_user.id)

        # Verify the new event was NOT created
        stmt = select(FeedingEvent).where(FeedingEvent.id == new_event_id)
        result = await async_session.execute(stmt)
        assert result.scalar_one_or_none() is None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_feeding_outside_window_no_conflict(
    async_session: AsyncSession,
):
    """Test that events outside 5min window are processed normally."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        schedule = await create_test_feeding_schedule(async_session, aquarium.id)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Create a completed feeding event by owner
        now = datetime.now(UTC)
        await create_completed_feeding_event(
            async_session,
            aquarium.id,
            schedule.id,
            completed_at=now,
            completed_by=owner.id,
        )

        # Member syncs a feeding event OUTSIDE 5min window (10 minutes later)
        client_completed_at = now + timedelta(minutes=10)
        new_event_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="event",
            entity_id=new_event_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "scheduled_at": client_completed_at.isoformat(),
                "status": "completed",
                "completed_at": client_completed_at.isoformat(),
                "completed_by": str(member_user.id),
            },
            client_updated_at=client_completed_at,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, member_user.id, request)

        # No concurrent_feeding conflict
        assert len(response.conflicts) == 0

        # Verify the new event was created
        stmt = select(FeedingEvent).where(FeedingEvent.id == new_event_id)
        result = await async_session.execute(stmt)
        created_event = result.scalar_one_or_none()
        assert created_event is not None
        assert created_event.status == "completed"
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_feeding_same_user_no_conflict(
    async_session: AsyncSession,
):
    """Test that same user's events don't trigger concurrent_feeding conflict."""
    await cleanup_sync_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        schedule = await create_test_feeding_schedule(async_session, aquarium.id)

        # Create a completed feeding event by user
        now = datetime.now(UTC)
        await create_completed_feeding_event(
            async_session,
            aquarium.id,
            schedule.id,
            completed_at=now,
            completed_by=user.id,
        )

        # Same user syncs another feeding event within window
        client_completed_at = now + timedelta(minutes=2)
        new_event_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="event",
            entity_id=new_event_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "scheduled_at": now.isoformat(),
                "status": "completed",
                "completed_at": client_completed_at.isoformat(),
                "completed_by": str(user.id),  # Same user
            },
            client_updated_at=client_completed_at,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, user.id, request)

        # No concurrent_feeding conflict (same user)
        assert len(response.conflicts) == 0

        # Event should be created
        stmt = select(FeedingEvent).where(FeedingEvent.id == new_event_id)
        result = await async_session.execute(stmt)
        assert result.scalar_one_or_none() is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_feeding_no_schedule_no_conflict(
    async_session: AsyncSession,
):
    """Test that ad-hoc feedings (no schedule_id) don't trigger concurrent_feeding."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        # Create an ad-hoc completed feeding event (no schedule_id)
        now = datetime.now(UTC)
        adhoc_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=None,  # Ad-hoc feeding
            scheduled_at=now,
            status="completed",
            completed_at=now,
            completed_by=owner.id,
        )
        async_session.add(adhoc_event)
        await async_session.commit()

        # Member syncs another ad-hoc feeding within window
        client_completed_at = now + timedelta(minutes=2)
        new_event_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="event",
            entity_id=new_event_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                # No schedule_id - ad-hoc feeding
                "scheduled_at": now.isoformat(),
                "status": "completed",
                "completed_at": client_completed_at.isoformat(),
                "completed_by": str(member_user.id),
            },
            client_updated_at=client_completed_at,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, member_user.id, request)

        # No concurrent_feeding conflict for ad-hoc feedings
        assert len(response.conflicts) == 0

        # Event should be created
        stmt = select(FeedingEvent).where(FeedingEvent.id == new_event_id)
        result = await async_session.execute(stmt)
        assert result.scalar_one_or_none() is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_feeding_update_operation(
    async_session: AsyncSession,
):
    """Test that UPDATE to completed status triggers concurrent_feeding detection."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        schedule = await create_test_feeding_schedule(async_session, aquarium.id)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        now = datetime.now(UTC)

        # Create a pending event that member will try to complete
        pending_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=now,
            status="pending",
        )
        async_session.add(pending_event)
        await async_session.commit()
        await async_session.refresh(pending_event)

        # Owner completes a feeding (creates conflict)
        owner_event = await create_completed_feeding_event(
            async_session,
            aquarium.id,
            schedule.id,
            completed_at=now + timedelta(minutes=1),
            completed_by=owner.id,
        )

        # Member tries to UPDATE the pending event to completed within window
        client_completed_at = now + timedelta(minutes=2)

        change = ChangeItem(
            entity_type="event",
            entity_id=pending_event.id,
            operation="update",
            data={
                "status": "completed",
                "completed_at": client_completed_at.isoformat(),
                "completed_by": str(member_user.id),
            },
            client_updated_at=pending_event.updated_at + timedelta(hours=1),
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, member_user.id, request)

        # Should have concurrent_feeding conflict
        assert len(response.conflicts) == 1
        conflict = response.conflicts[0]
        assert conflict.resolution == "concurrent_feeding"
        assert conflict.server_data["id"] == str(owner_event.id)
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_feeding_different_schedules_no_conflict(
    async_session: AsyncSession,
):
    """Test that events for different schedules don't trigger concurrent_feeding."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        schedule1 = await create_test_feeding_schedule(async_session, aquarium.id)
        schedule2 = await create_test_feeding_schedule(async_session, aquarium.id)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        now = datetime.now(UTC)

        # Owner completes feeding for schedule1
        await create_completed_feeding_event(
            async_session,
            aquarium.id,
            schedule1.id,  # Different schedule
            completed_at=now,
            completed_by=owner.id,
        )

        # Member syncs feeding for schedule2 within window
        client_completed_at = now + timedelta(minutes=2)
        new_event_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="event",
            entity_id=new_event_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule2.id),  # Different schedule
                "scheduled_at": now.isoformat(),
                "status": "completed",
                "completed_at": client_completed_at.isoformat(),
                "completed_by": str(member_user.id),
            },
            client_updated_at=client_completed_at,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, member_user.id, request)

        # No conflict - different schedules
        assert len(response.conflicts) == 0

        # Event should be created
        stmt = select(FeedingEvent).where(FeedingEvent.id == new_event_id)
        result = await async_session.execute(stmt)
        assert result.scalar_one_or_none() is not None
    finally:
        await cleanup_sync_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_feeding_pending_event_no_conflict(
    async_session: AsyncSession,
):
    """Test that pending events (not completed) don't trigger concurrent_feeding."""
    await cleanup_sync_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        member_user = await create_test_user(async_session, "member@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)
        schedule = await create_test_feeding_schedule(async_session, aquarium.id)

        # Add member_user as member
        member = AquariumMember(
            aquarium_id=aquarium.id,
            user_id=member_user.id,
            role="member",
        )
        async_session.add(member)
        await async_session.commit()

        now = datetime.now(UTC)

        # Create a PENDING event (not completed)
        pending_event = FeedingEvent(
            aquarium_id=aquarium.id,
            schedule_id=schedule.id,
            scheduled_at=now,
            status="pending",  # Not completed
        )
        async_session.add(pending_event)
        await async_session.commit()

        # Member syncs a completed feeding event within window
        client_completed_at = now + timedelta(minutes=2)
        new_event_id = uuid.uuid4()

        change = ChangeItem(
            entity_type="event",
            entity_id=new_event_id,
            operation="create",
            data={
                "aquarium_id": str(aquarium.id),
                "schedule_id": str(schedule.id),
                "scheduled_at": now.isoformat(),
                "status": "completed",
                "completed_at": client_completed_at.isoformat(),
                "completed_by": str(member_user.id),
            },
            client_updated_at=client_completed_at,
        )
        request = SyncRequest(changes=[change], last_sync_at=None)

        response = await process_sync(async_session, member_user.id, request)

        # No conflict - existing event was pending, not completed
        assert len(response.conflicts) == 0

        # Event should be created
        stmt = select(FeedingEvent).where(FeedingEvent.id == new_event_id)
        result = await async_session.execute(stmt)
        assert result.scalar_one_or_none() is not None
    finally:
        await cleanup_sync_test_data(async_session)
