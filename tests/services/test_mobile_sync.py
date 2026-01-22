"""Unit tests for mobile sync service."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingEvent
from app.models.user import User
from app.schemas.sync import MobileFeedingEvent, MobileSyncRequest
from app.services.mobile_sync import (
    _feeding_event_to_mobile_dict,
    _get_server_events_since,
    _map_mobile_event_to_feeding_event_data,
    process_mobile_sync,
)
from app.services.sync import SyncAccessDeniedError, SyncValidationError


async def cleanup_test_data(session: AsyncSession) -> None:
    """Helper to cleanup test data."""
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


async def create_test_feeding_event(
    session: AsyncSession,
    aquarium_id: uuid.UUID,
    scheduled_at: datetime | None = None,
    status: str = "pending",
    completed_by: uuid.UUID | None = None,
    completed_at: datetime | None = None,
) -> FeedingEvent:
    """Helper to create a test feeding event."""
    event = FeedingEvent(
        aquarium_id=aquarium_id,
        scheduled_at=scheduled_at or datetime.now(UTC),
        status=status,
        completed_by=completed_by,
        completed_at=completed_at,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


# ============================================================================
# Unit tests for _feeding_event_to_mobile_dict
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_to_mobile_dict_pending_event(async_session: AsyncSession):
    """Test conversion of pending feeding event to mobile dict."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        scheduled_at = datetime.now(UTC)
        event = await create_test_feeding_event(
            async_session, aquarium.id, scheduled_at=scheduled_at
        )

        result = _feeding_event_to_mobile_dict(event)

        assert result["id"] == str(event.id)
        assert result["aquarium_id"] == str(aquarium.id)
        assert result["feeding_time"] == scheduled_at.isoformat()
        assert result["status"] == "pending"
        assert result["completed_at"] is None
        assert result["completed_by"] is None
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_feeding_event_to_mobile_dict_completed_event(async_session: AsyncSession):
    """Test conversion of completed feeding event to mobile dict."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        scheduled_at = datetime.now(UTC)
        completed_at = datetime.now(UTC)
        event = await create_test_feeding_event(
            async_session,
            aquarium.id,
            scheduled_at=scheduled_at,
            status="completed",
            completed_by=user.id,
            completed_at=completed_at,
        )

        result = _feeding_event_to_mobile_dict(event)

        assert result["id"] == str(event.id)
        assert result["status"] == "completed"
        assert result["completed_by"] == str(user.id)
        assert result["completed_at"] == completed_at.isoformat()
    finally:
        await cleanup_test_data(async_session)


# ============================================================================
# Unit tests for _map_mobile_event_to_feeding_event_data
# ============================================================================


def test_map_mobile_event_pending():
    """Test mapping mobile event without completed_by to pending status."""
    user_id = uuid.uuid4()
    feeding_time = datetime.now(UTC)
    aquarium_id = str(uuid.uuid4())

    mobile_event = MobileFeedingEvent(
        id=str(uuid.uuid4()),
        aquarium_id=aquarium_id,
        feeding_time=feeding_time,
        created_at=datetime.now(UTC),
    )

    result = _map_mobile_event_to_feeding_event_data(mobile_event, user_id)

    assert result["scheduled_at"] == feeding_time
    assert result["status"] == "pending"
    assert result["aquarium_id"] == uuid.UUID(aquarium_id)
    assert "completed_by" not in result
    assert "completed_at" not in result


def test_map_mobile_event_completed():
    """Test mapping mobile event with completed_by to completed status."""
    user_id = uuid.uuid4()
    feeding_time = datetime.now(UTC)
    aquarium_id = str(uuid.uuid4())
    completed_by_id = str(uuid.uuid4())

    mobile_event = MobileFeedingEvent(
        id=str(uuid.uuid4()),
        aquarium_id=aquarium_id,
        feeding_time=feeding_time,
        created_at=datetime.now(UTC),
        completed_by=completed_by_id,
    )

    result = _map_mobile_event_to_feeding_event_data(mobile_event, user_id)

    assert result["scheduled_at"] == feeding_time
    assert result["status"] == "completed"
    assert result["completed_by"] == uuid.UUID(completed_by_id)
    assert result["completed_at"] == feeding_time


# ============================================================================
# Unit tests for _get_server_events_since
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_events_since_returns_updated_events(async_session: AsyncSession):
    """Test that _get_server_events_since returns events updated after timestamp."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Create an event
        event1 = await create_test_feeding_event(async_session, aquarium.id)

        # Record time after event1
        since_time = event1.updated_at + timedelta(microseconds=1)

        # Create another event after since_time
        event2 = await create_test_feeding_event(async_session, aquarium.id)

        result = await _get_server_events_since(
            async_session, {aquarium.id}, since_time
        )

        # Should only return event2
        assert len(result) >= 1
        event_ids = {e["id"] for e in result}
        assert str(event2.id) in event_ids
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_server_events_since_empty_aquariums(async_session: AsyncSession):
    """Test that empty aquarium set returns empty list."""
    result = await _get_server_events_since(async_session, set(), datetime.now(UTC))
    assert result == []


# ============================================================================
# Tests for process_mobile_sync - empty events
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_empty_events_returns_empty_response(async_session: AsyncSession):
    """Test that mobile sync with no events returns empty response."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)

        request = MobileSyncRequest(
            events=[],
            client_timestamp=datetime.now(UTC),
        )

        response = await process_mobile_sync(async_session, user.id, request)

        assert response.synced_ids == []
        assert response.server_events == []
    finally:
        await cleanup_test_data(async_session)


# ============================================================================
# Tests for process_mobile_sync - creating new events
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_saves_new_events_to_database(async_session: AsyncSession):
    """Test that mobile sync saves new events to database."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        event_id = str(uuid.uuid4())
        feeding_time = datetime.now(UTC)

        mobile_event = MobileFeedingEvent(
            id=event_id,
            aquarium_id=str(aquarium.id),
            feeding_time=feeding_time,
            created_at=datetime.now(UTC),
        )

        request = MobileSyncRequest(
            events=[mobile_event],
            client_timestamp=datetime.now(UTC) - timedelta(hours=1),
        )

        response = await process_mobile_sync(async_session, user.id, request)

        # Check synced_ids
        assert event_id in response.synced_ids

        # Verify event was saved to database
        stmt = select(FeedingEvent).where(FeedingEvent.id == uuid.UUID(event_id))
        result = await async_session.execute(stmt)
        saved_event = result.scalar_one_or_none()

        assert saved_event is not None
        assert saved_event.aquarium_id == aquarium.id
        assert saved_event.status == "pending"
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_returns_synced_ids_for_saved_events(async_session: AsyncSession):
    """Test that mobile sync returns synced_ids for all saved events."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        now = datetime.now(UTC)

        event_ids = [str(uuid.uuid4()) for _ in range(3)]
        events = [
            MobileFeedingEvent(
                id=eid,
                aquarium_id=str(aquarium.id),
                feeding_time=now,
                created_at=now,
            )
            for eid in event_ids
        ]

        request = MobileSyncRequest(
            events=events,
            client_timestamp=now - timedelta(hours=1),
        )

        response = await process_mobile_sync(async_session, user.id, request)

        # All events should be in synced_ids
        assert set(response.synced_ids) == set(event_ids)
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_completed_event_sets_correct_fields(async_session: AsyncSession):
    """Test that completed event has correct status, completed_by, and completed_at."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        event_id = str(uuid.uuid4())
        feeding_time = datetime.now(UTC)

        mobile_event = MobileFeedingEvent(
            id=event_id,
            aquarium_id=str(aquarium.id),
            feeding_time=feeding_time,
            created_at=datetime.now(UTC),
            completed_by=str(user.id),
        )

        request = MobileSyncRequest(
            events=[mobile_event],
            client_timestamp=datetime.now(UTC) - timedelta(hours=1),
        )

        await process_mobile_sync(async_session, user.id, request)

        # Verify event fields
        stmt = select(FeedingEvent).where(FeedingEvent.id == uuid.UUID(event_id))
        result = await async_session.execute(stmt)
        saved_event = result.scalar_one_or_none()

        assert saved_event is not None
        assert saved_event.status == "completed"
        assert saved_event.completed_by == user.id
        assert saved_event.completed_at == feeding_time
    finally:
        await cleanup_test_data(async_session)


# ============================================================================
# Tests for process_mobile_sync - server_events (delta query)
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_returns_server_events_after_client_timestamp(
    async_session: AsyncSession,
):
    """Test that server_events contains events updated after client_timestamp."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Create an event on the server
        server_event = await create_test_feeding_event(async_session, aquarium.id)

        # Client timestamp is before the server event
        client_timestamp = server_event.updated_at - timedelta(hours=1)

        request = MobileSyncRequest(
            events=[],
            client_timestamp=client_timestamp,
        )

        response = await process_mobile_sync(async_session, user.id, request)

        # server_events should contain the server event
        assert len(response.server_events) >= 1
        event_ids = {e["id"] for e in response.server_events}
        assert str(server_event.id) in event_ids
    finally:
        await cleanup_test_data(async_session)


# ============================================================================
# Tests for process_mobile_sync - last-write-wins
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_last_write_wins_client_newer(async_session: AsyncSession):
    """Test that client wins when client timestamp is newer."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Create existing event on server
        server_event = await create_test_feeding_event(
            async_session, aquarium.id, status="pending"
        )

        # Client sends update with newer timestamp
        client_time = server_event.updated_at + timedelta(hours=1)

        mobile_event = MobileFeedingEvent(
            id=str(server_event.id),
            aquarium_id=str(aquarium.id),
            feeding_time=datetime.now(UTC),
            created_at=datetime.now(UTC),
            updated_at=client_time,
            completed_by=str(user.id),
        )

        request = MobileSyncRequest(
            events=[mobile_event],
            client_timestamp=datetime.now(UTC) - timedelta(hours=2),
        )

        response = await process_mobile_sync(async_session, user.id, request)

        # Event should be in synced_ids (client wins)
        assert str(server_event.id) in response.synced_ids

        # Verify server event was updated
        await async_session.refresh(server_event)
        assert server_event.status == "completed"
        assert server_event.completed_by == user.id
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_last_write_wins_server_newer(async_session: AsyncSession):
    """Test that server wins when server timestamp is newer."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Create existing event on server
        server_event = await create_test_feeding_event(
            async_session, aquarium.id, status="completed"
        )
        original_status = server_event.status

        # Client sends update with OLDER timestamp
        client_time = server_event.updated_at - timedelta(hours=1)

        mobile_event = MobileFeedingEvent(
            id=str(server_event.id),
            aquarium_id=str(aquarium.id),
            feeding_time=datetime.now(UTC),
            created_at=client_time,
            updated_at=client_time,
            # Trying to set status to pending by not providing completed_by
        )

        request = MobileSyncRequest(
            events=[mobile_event],
            client_timestamp=datetime.now(UTC) - timedelta(hours=2),
        )

        response = await process_mobile_sync(async_session, user.id, request)

        # Event should NOT be in synced_ids (server wins)
        assert str(server_event.id) not in response.synced_ids

        # Verify server event was NOT changed
        await async_session.refresh(server_event)
        assert server_event.status == original_status
    finally:
        await cleanup_test_data(async_session)


# ============================================================================
# Tests for process_mobile_sync - access control
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_access_denied_for_other_user_aquarium(
    async_session: AsyncSession,
):
    """Test that sync is denied for events in other user's aquarium."""
    await cleanup_test_data(async_session)
    try:
        owner = await create_test_user(async_session, "owner@example.com")
        other_user = await create_test_user(async_session, "other@example.com")
        aquarium = await create_test_aquarium(async_session, owner.id)

        mobile_event = MobileFeedingEvent(
            id=str(uuid.uuid4()),
            aquarium_id=str(aquarium.id),  # Other user's aquarium
            feeding_time=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )

        request = MobileSyncRequest(
            events=[mobile_event],
            client_timestamp=datetime.now(UTC),
        )

        with pytest.raises(SyncAccessDeniedError):
            await process_mobile_sync(async_session, other_user.id, request)
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_validation_error_for_missing_aquarium_id(
    async_session: AsyncSession,
):
    """Test that validation error is raised when aquarium_id is missing for new event."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)

        mobile_event = MobileFeedingEvent(
            id=str(uuid.uuid4()),
            aquarium_id=None,  # Missing aquarium_id
            feeding_time=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )

        request = MobileSyncRequest(
            events=[mobile_event],
            client_timestamp=datetime.now(UTC),
        )

        with pytest.raises(SyncValidationError) as exc_info:
            await process_mobile_sync(async_session, user.id, request)

        assert "aquarium_id" in exc_info.value.message
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_validation_error_for_invalid_aquarium_id(
    async_session: AsyncSession,
):
    """Test that validation error is raised for invalid aquarium_id format."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)

        mobile_event = MobileFeedingEvent(
            id=str(uuid.uuid4()),
            aquarium_id="not-a-uuid",  # Invalid UUID
            feeding_time=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )

        request = MobileSyncRequest(
            events=[mobile_event],
            client_timestamp=datetime.now(UTC),
        )

        with pytest.raises(SyncValidationError) as exc_info:
            await process_mobile_sync(async_session, user.id, request)

        assert "aquarium_id" in exc_info.value.message
    finally:
        await cleanup_test_data(async_session)


# ============================================================================
# Tests for multi-device scenarios
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_multi_device_scenario(async_session: AsyncSession):
    """Test multi-device sync: Device A syncs, Device B gets server_events."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Device A's initial timestamp (before any events)
        device_a_timestamp = datetime.now(UTC) - timedelta(hours=2)
        device_b_timestamp = datetime.now(UTC) - timedelta(hours=2)

        # Device A creates and syncs an event
        event_id = str(uuid.uuid4())
        device_a_event = MobileFeedingEvent(
            id=event_id,
            aquarium_id=str(aquarium.id),
            feeding_time=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )

        request_a = MobileSyncRequest(
            events=[device_a_event],
            client_timestamp=device_a_timestamp,
        )

        response_a = await process_mobile_sync(async_session, user.id, request_a)
        assert event_id in response_a.synced_ids

        # Device B syncs with no events (just to get server state)
        request_b = MobileSyncRequest(
            events=[],
            client_timestamp=device_b_timestamp,
        )

        response_b = await process_mobile_sync(async_session, user.id, request_b)

        # Device B should receive the event from Device A
        server_event_ids = {e["id"] for e in response_b.server_events}
        assert event_id in server_event_ids
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_conflict_resolution_multi_device(async_session: AsyncSession):
    """Test conflict resolution: Device A syncs first, Device B with older timestamp loses."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        event_id = str(uuid.uuid4())
        base_time = datetime.now(UTC)

        # Device A syncs event first (newer timestamp)
        device_a_event = MobileFeedingEvent(
            id=event_id,
            aquarium_id=str(aquarium.id),
            feeding_time=base_time,
            created_at=base_time,
            updated_at=base_time + timedelta(minutes=5),  # Device A is newer
            completed_by=str(user.id),
        )

        request_a = MobileSyncRequest(
            events=[device_a_event],
            client_timestamp=base_time - timedelta(hours=1),
        )

        response_a = await process_mobile_sync(async_session, user.id, request_a)
        assert event_id in response_a.synced_ids

        # Device B syncs same event with OLDER timestamp
        device_b_event = MobileFeedingEvent(
            id=event_id,
            aquarium_id=str(aquarium.id),
            feeding_time=base_time,
            created_at=base_time - timedelta(minutes=10),
            updated_at=base_time - timedelta(minutes=5),  # Device B is older
            # Device B has pending status (no completed_by)
        )

        request_b = MobileSyncRequest(
            events=[device_b_event],
            client_timestamp=base_time - timedelta(hours=1),
        )

        response_b = await process_mobile_sync(async_session, user.id, request_b)

        # Device B's event should NOT be in synced_ids (server wins)
        assert event_id not in response_b.synced_ids

        # Verify the event still has Device A's data (completed)
        stmt = select(FeedingEvent).where(FeedingEvent.id == uuid.UUID(event_id))
        result = await async_session.execute(stmt)
        saved_event = result.scalar_one_or_none()

        assert saved_event is not None
        assert saved_event.status == "completed"
        assert saved_event.completed_by == user.id
    finally:
        await cleanup_test_data(async_session)


# ============================================================================
# Performance tests
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_100_events_performance(async_session: AsyncSession):
    """Test that sync with 100 events completes successfully."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)
        now = datetime.now(UTC)

        # Create 100 events
        events = [
            MobileFeedingEvent(
                id=str(uuid.uuid4()),
                aquarium_id=str(aquarium.id),
                feeding_time=now + timedelta(minutes=i),
                created_at=now,
            )
            for i in range(100)
        ]

        request = MobileSyncRequest(
            events=events,
            client_timestamp=now - timedelta(hours=1),
        )

        response = await process_mobile_sync(async_session, user.id, request)

        # All 100 events should be synced
        assert len(response.synced_ids) == 100
    finally:
        await cleanup_test_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_delta_sync_does_not_load_all_events(
    async_session: AsyncSession,
):
    """Test that delta sync only returns events after client_timestamp."""
    await cleanup_test_data(async_session)
    try:
        user = await create_test_user(async_session)
        aquarium = await create_test_aquarium(async_session, user.id)

        # Create 10 old events
        old_events = []
        for _ in range(10):
            event = await create_test_feeding_event(async_session, aquarium.id)
            old_events.append(event)

        # Use the last old event's updated_at as the midpoint
        # Add a small offset to ensure new events are "after"
        last_old_event = old_events[-1]
        midpoint = last_old_event.updated_at + timedelta(microseconds=1)

        # Create 5 new events
        new_events = []
        for _ in range(5):
            event = await create_test_feeding_event(async_session, aquarium.id)
            new_events.append(event)

        request = MobileSyncRequest(
            events=[],
            client_timestamp=midpoint,
        )

        response = await process_mobile_sync(async_session, user.id, request)

        # Should return at least some new events but not the old ones
        server_event_ids = {e["id"] for e in response.server_events}

        # Check that new events are included
        for new_event in new_events:
            assert str(new_event.id) in server_event_ids

        # Check that old events are NOT included (they were created before midpoint)
        for old_event in old_events:
            assert str(old_event.id) not in server_event_ids
    finally:
        await cleanup_test_data(async_session)


# ============================================================================
# Tests for member access
# ============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_mobile_sync_member_can_sync_events(async_session: AsyncSession):
    """Test that aquarium members can sync events."""
    await cleanup_test_data(async_session)
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

        # Member creates an event
        event_id = str(uuid.uuid4())
        mobile_event = MobileFeedingEvent(
            id=event_id,
            aquarium_id=str(aquarium.id),
            feeding_time=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )

        request = MobileSyncRequest(
            events=[mobile_event],
            client_timestamp=datetime.now(UTC) - timedelta(hours=1),
        )

        response = await process_mobile_sync(async_session, member_user.id, request)

        # Member should be able to sync
        assert event_id in response.synced_ids
    finally:
        await cleanup_test_data(async_session)
