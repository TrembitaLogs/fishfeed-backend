"""Tests for analytics service."""

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.analytics import AnalyticsEvent
from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak
from app.models.user import User
from app.schemas.analytics import EventRequest
from app.services.analytics import (
    BatchSizeExceededError,
    GDPRError,
    UserNotFoundError,
    delete_user_data,
    export_user_data,
    forward_to_external,
    hash_ip,
    track_event,
    track_events_batch,
)


async def cleanup_analytics_data(session: AsyncSession) -> None:
    """Helper to cleanup analytics-related data."""
    await session.execute(text("DELETE FROM analytics_events"))
    await session.commit()


async def create_test_user(session: AsyncSession) -> User:
    """Helper to create a test user."""
    user = User(
        email=f"analytics-test-{uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class TestHashIp:
    """Tests for IP hashing function."""

    def test_hash_ip_produces_64_char_hex(self):
        """Test that hash_ip produces a 64-character hex string."""
        result = hash_ip("192.168.1.1")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_hash_ip_consistent(self):
        """Test that same IP produces same hash."""
        ip = "10.0.0.1"
        hash1 = hash_ip(ip)
        hash2 = hash_ip(ip)
        assert hash1 == hash2

    def test_hash_ip_different_ips_different_hashes(self):
        """Test that different IPs produce different hashes."""
        hash1 = hash_ip("192.168.1.1")
        hash2 = hash_ip("192.168.1.2")
        assert hash1 != hash2

    def test_hash_ip_uses_salt(self):
        """Test that IP hashing uses the configured salt."""
        ip = "127.0.0.1"
        settings = get_settings()
        expected_salted = f"{settings.ANALYTICS_IP_SALT}:{ip}"
        expected_hash = hashlib.sha256(expected_salted.encode()).hexdigest()
        assert hash_ip(ip) == expected_hash


@pytest.mark.asyncio(loop_scope="session")
class TestTrackEvent:
    """Tests for track_event function."""

    async def test_track_event_saves_to_db(self, async_session: AsyncSession):
        """Test that track_event saves event to database."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        event = EventRequest(
            event_type="button_click",
            properties={"button_id": "submit"},
        )
        ip = "192.168.1.100"

        await track_event(async_session, user.id, event, ip)

        # Verify event was saved
        stmt = select(AnalyticsEvent).where(AnalyticsEvent.user_id == user.id)
        result = await async_session.execute(stmt)
        saved_event = result.scalar_one_or_none()

        assert saved_event is not None
        assert saved_event.event_type == "button_click"
        assert saved_event.properties == {"button_id": "submit"}
        assert saved_event.ip_hash == hash_ip(ip)
        assert saved_event.user_id == user.id

    async def test_track_event_with_device_info(self, async_session: AsyncSession):
        """Test that device_info is saved correctly."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        event = EventRequest(
            event_type="page_view",
            properties={"page": "/home"},
            device_info={"os": "iOS", "version": "17.0"},
        )

        await track_event(async_session, user.id, event, "10.0.0.1")

        stmt = select(AnalyticsEvent).where(AnalyticsEvent.user_id == user.id)
        result = await async_session.execute(stmt)
        saved_event = result.scalar_one()

        assert saved_event.device_info == {"os": "iOS", "version": "17.0"}

    async def test_track_event_with_custom_timestamp(self, async_session: AsyncSession):
        """Test that custom timestamp is used when provided."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        custom_time = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        event = EventRequest(
            event_type="test_event",
            timestamp=custom_time,
        )

        await track_event(async_session, user.id, event, "10.0.0.1")

        stmt = select(AnalyticsEvent).where(AnalyticsEvent.user_id == user.id)
        result = await async_session.execute(stmt)
        saved_event = result.scalar_one()

        assert saved_event.created_at == custom_time

    async def test_track_event_uses_server_time_when_no_timestamp(
        self, async_session: AsyncSession
    ):
        """Test that server time is used when no timestamp provided."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        before = datetime.now(UTC)

        event = EventRequest(event_type="test_event")
        await track_event(async_session, user.id, event, "10.0.0.1")

        after = datetime.now(UTC)

        stmt = select(AnalyticsEvent).where(AnalyticsEvent.user_id == user.id)
        result = await async_session.execute(stmt)
        saved_event = result.scalar_one()

        assert before <= saved_event.created_at <= after

    @patch("app.services.analytics.get_settings")
    async def test_track_event_triggers_forward_when_configured(
        self, mock_settings, async_session: AsyncSession
    ):
        """Test that external forwarding is triggered when URL is configured."""
        await cleanup_analytics_data(async_session)
        mock_settings.return_value.ANALYTICS_FORWARD_URL = "https://analytics.example.com/events"
        mock_settings.return_value.ANALYTICS_IP_SALT = "test-salt"
        mock_settings.return_value.ANALYTICS_FORWARD_TIMEOUT_SECONDS = 10
        mock_settings.return_value.ANALYTICS_FORWARD_MAX_RETRIES = 3

        user = await create_test_user(async_session)
        event = EventRequest(event_type="test_event")

        with patch("app.services.analytics.asyncio.create_task") as mock_create_task:
            await track_event(async_session, user.id, event, "10.0.0.1")
            # Verify create_task was called (fire-and-forget)
            mock_create_task.assert_called_once()


@pytest.mark.asyncio(loop_scope="session")
class TestTrackEventsBatch:
    """Tests for track_events_batch function."""

    async def test_batch_saves_multiple_events(self, async_session: AsyncSession):
        """Test that batch saves all events to database."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        events = [
            EventRequest(event_type="event_1", properties={"index": 1}),
            EventRequest(event_type="event_2", properties={"index": 2}),
            EventRequest(event_type="event_3", properties={"index": 3}),
        ]

        count = await track_events_batch(async_session, user.id, events, "10.0.0.1")

        assert count == 3

        stmt = select(AnalyticsEvent).where(AnalyticsEvent.user_id == user.id)
        result = await async_session.execute(stmt)
        saved_events = list(result.scalars().all())

        assert len(saved_events) == 3
        event_types = {e.event_type for e in saved_events}
        assert event_types == {"event_1", "event_2", "event_3"}

    async def test_batch_max_100_events_accepted(self, async_session: AsyncSession):
        """Test that batch with exactly 100 events is accepted."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        events = [
            EventRequest(event_type=f"event_{i}")
            for i in range(100)
        ]

        count = await track_events_batch(async_session, user.id, events, "10.0.0.1")

        assert count == 100

    async def test_batch_over_100_events_rejected(self, async_session: AsyncSession):
        """Test that batch with >100 events raises BatchSizeExceededError."""
        user = await create_test_user(async_session)
        events = [
            EventRequest(event_type=f"event_{i}")
            for i in range(101)
        ]

        with pytest.raises(BatchSizeExceededError) as exc_info:
            await track_events_batch(async_session, user.id, events, "10.0.0.1")

        assert exc_info.value.status_code == 400
        assert "101" in str(exc_info.value.message)
        assert "100" in str(exc_info.value.message)

    async def test_batch_empty_returns_zero(self, async_session: AsyncSession):
        """Test that empty batch returns 0."""
        user = await create_test_user(async_session)

        count = await track_events_batch(async_session, user.id, [], "10.0.0.1")

        assert count == 0

    async def test_batch_all_events_have_same_ip_hash(self, async_session: AsyncSession):
        """Test that all events in batch have the same IP hash."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        ip = "192.168.50.1"
        events = [
            EventRequest(event_type=f"event_{i}")
            for i in range(5)
        ]

        await track_events_batch(async_session, user.id, events, ip)

        stmt = select(AnalyticsEvent).where(AnalyticsEvent.user_id == user.id)
        result = await async_session.execute(stmt)
        saved_events = list(result.scalars().all())

        expected_hash = hash_ip(ip)
        assert all(e.ip_hash == expected_hash for e in saved_events)

    async def test_batch_returns_count_of_saved_events(self, async_session: AsyncSession):
        """Test that batch returns correct count."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        events = [EventRequest(event_type="event") for _ in range(42)]

        count = await track_events_batch(async_session, user.id, events, "10.0.0.1")

        assert count == 42


@pytest.mark.asyncio(loop_scope="session")
class TestForwardToExternal:
    """Tests for forward_to_external function."""

    @patch("app.services.analytics.get_settings")
    @patch("app.services.analytics.httpx.AsyncClient")
    async def test_forward_sends_correct_payload(
        self, mock_client_class, mock_settings
    ):
        """Test that forward sends correct payload structure."""
        mock_settings.return_value.ANALYTICS_FORWARD_URL = "https://analytics.example.com/events"
        mock_settings.return_value.ANALYTICS_FORWARD_TIMEOUT_SECONDS = 10
        mock_settings.return_value.ANALYTICS_FORWARD_MAX_RETRIES = 3

        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.raise_for_status = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)

        events = [
            {"user_id": "user-123", "event_type": "test", "properties": {}}
        ]

        await forward_to_external(events)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args.kwargs["json"] == {"events": events}
        assert call_args.kwargs["headers"]["Content-Type"] == "application/json"

    @patch("app.services.analytics.get_settings")
    async def test_forward_skipped_when_url_not_configured(self, mock_settings):
        """Test that forwarding is skipped when URL is not configured."""
        mock_settings.return_value.ANALYTICS_FORWARD_URL = None

        # Should not raise, should just return
        await forward_to_external([{"event": "test"}])

    @patch("app.services.analytics.get_settings")
    @patch("app.services.analytics.httpx.AsyncClient")
    @patch("app.services.analytics.asyncio.sleep")
    async def test_forward_retries_on_failure(
        self, mock_sleep, mock_client_class, mock_settings
    ):
        """Test that forward retries on HTTP errors."""
        mock_settings.return_value.ANALYTICS_FORWARD_URL = "https://analytics.example.com/events"
        mock_settings.return_value.ANALYTICS_FORWARD_TIMEOUT_SECONDS = 10
        mock_settings.return_value.ANALYTICS_FORWARD_MAX_RETRIES = 3

        mock_client = AsyncMock()
        # Fail twice, succeed on third
        import httpx
        mock_client.post = AsyncMock(
            side_effect=[
                httpx.HTTPError("Error 1"),
                httpx.HTTPError("Error 2"),
                AsyncMock(raise_for_status=AsyncMock()),
            ]
        )
        mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)

        await forward_to_external([{"event": "test"}])

        assert mock_client.post.call_count == 3
        # Verify exponential backoff sleep calls
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)  # 2^0
        mock_sleep.assert_any_call(2)  # 2^1

    @patch("app.services.analytics.get_settings")
    @patch("app.services.analytics.httpx.AsyncClient")
    @patch("app.services.analytics.asyncio.sleep")
    async def test_forward_raises_after_max_retries(
        self, mock_sleep, mock_client_class, mock_settings
    ):
        """Test that forward raises after max retries exceeded."""
        mock_settings.return_value.ANALYTICS_FORWARD_URL = "https://analytics.example.com/events"
        mock_settings.return_value.ANALYTICS_FORWARD_TIMEOUT_SECONDS = 10
        mock_settings.return_value.ANALYTICS_FORWARD_MAX_RETRIES = 3

        mock_client = AsyncMock()
        import httpx
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("Persistent error"))
        mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)

        with pytest.raises(httpx.HTTPError):
            await forward_to_external([{"event": "test"}])

        assert mock_client.post.call_count == 3


@pytest.mark.asyncio(loop_scope="session")
class TestAnalyticsEventModel:
    """Tests for AnalyticsEvent model and indexes."""

    async def test_analytics_event_created_with_defaults(
        self, async_session: AsyncSession
    ):
        """Test AnalyticsEvent creation with default values."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        event = AnalyticsEvent(
            user_id=user.id,
            event_type="test_event",
            properties={},
            ip_hash="a" * 64,
        )
        async_session.add(event)
        await async_session.commit()

        assert event.id is not None
        assert event.created_at is not None
        assert event.anonymized_at is None
        assert event.device_info is None

    async def test_analytics_event_nullable_user_id(
        self, async_session: AsyncSession
    ):
        """Test AnalyticsEvent can have null user_id (for anonymized events)."""
        await cleanup_analytics_data(async_session)
        event = AnalyticsEvent(
            user_id=None,
            event_type="anonymous_event",
            properties={"source": "anonymous"},
            ip_hash="b" * 64,
        )
        async_session.add(event)
        await async_session.commit()

        assert event.id is not None
        assert event.user_id is None

    async def test_analytics_event_jsonb_properties(
        self, async_session: AsyncSession
    ):
        """Test that JSONB properties store complex data correctly."""
        await cleanup_analytics_data(async_session)
        user = await create_test_user(async_session)
        complex_props = {
            "string": "value",
            "number": 42,
            "float": 3.14,
            "boolean": True,
            "null": None,
            "array": [1, 2, 3],
            "nested": {"key": "value", "deep": {"level": 2}},
        }
        event = AnalyticsEvent(
            user_id=user.id,
            event_type="complex_event",
            properties=complex_props,
            ip_hash="c" * 64,
        )
        async_session.add(event)
        await async_session.commit()
        await async_session.refresh(event)

        assert event.properties == complex_props
        assert event.properties["nested"]["deep"]["level"] == 2


async def create_user_with_data(session: AsyncSession) -> User:
    """Helper to create a test user with various related data."""
    from app.models.species import Species

    user = User(
        email=f"gdpr-test-{uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.flush()

    # Create test species for this test
    test_species_id = f"gdpr-test-species-{uuid4().hex[:8]}"
    species = Species(
        id=test_species_id,
        common_name="GDPR Test Fish",
        scientific_name="Testus fishus",
        food_types=["flakes"],
        feeding_frequency=2,
        care_level="beginner",
        water_type="freshwater",
    )
    session.add(species)
    await session.flush()

    # Create aquarium
    aquarium = Aquarium(
        owner_id=user.id,
        name="Test Aquarium",
    )
    session.add(aquarium)
    await session.flush()

    # Create aquarium membership
    membership = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=user.id,
        role="owner",
    )
    session.add(membership)

    # Create fish
    fish = Fish(
        aquarium_id=aquarium.id,
        species_id=test_species_id,
        quantity=5,
        custom_name="Goldie",
    )
    session.add(fish)

    # Create streak
    streak = Streak(
        user_id=user.id,
        current_streak=10,
        best_streak=20,
    )
    session.add(streak)

    # Create achievement
    achievement = Achievement(
        user_id=user.id,
        achievement_type="first_feeding",
    )
    session.add(achievement)

    # Create analytics event
    event = AnalyticsEvent(
        user_id=user.id,
        event_type="test_event",
        properties={"test": True},
        ip_hash="d" * 64,
    )
    session.add(event)

    await session.commit()
    await session.refresh(user)
    return user


@pytest.mark.asyncio(loop_scope="session")
class TestExportUserData:
    """Tests for export_user_data function."""

    async def test_export_user_data_user_not_found(self, async_session: AsyncSession):
        """Test that export raises UserNotFoundError for non-existent user."""
        fake_user_id = uuid4()

        with pytest.raises(UserNotFoundError) as exc_info:
            mock_storage = MagicMock()
            await export_user_data(async_session, fake_user_id, mock_storage)

        assert exc_info.value.status_code == 404
        assert str(fake_user_id) in str(exc_info.value.message)

    async def test_export_user_data_returns_valid_response(
        self, async_session: AsyncSession
    ):
        """Test that export returns DataExportResponse with valid data."""
        user = await create_user_with_data(async_session)

        mock_storage = MagicMock()
        mock_storage.upload_json = AsyncMock(return_value="gdpr-exports/test/data.json")
        mock_storage.generate_presigned_url = AsyncMock(
            return_value="https://s3.example.com/presigned-url"
        )

        response = await export_user_data(async_session, user.id, mock_storage)

        assert str(response.download_url) == "https://s3.example.com/presigned-url"
        assert response.format == "json"
        assert response.file_size_bytes > 0
        assert response.expires_at is not None

    async def test_export_user_data_includes_all_tables(
        self, async_session: AsyncSession
    ):
        """Test that export includes data from all required tables."""
        user = await create_user_with_data(async_session)

        captured_json = None

        async def capture_upload(data, key):
            nonlocal captured_json
            captured_json = json.loads(data.decode("utf-8"))
            return key

        mock_storage = MagicMock()
        mock_storage.upload_json = AsyncMock(side_effect=capture_upload)
        mock_storage.generate_presigned_url = AsyncMock(
            return_value="https://s3.example.com/presigned-url"
        )

        await export_user_data(async_session, user.id, mock_storage)

        assert captured_json is not None

        # Verify profile data
        assert captured_json["profile"]["id"] == str(user.id)
        assert captured_json["profile"]["email"] == user.email

        # Verify aquarium data
        assert len(captured_json["owned_aquariums"]) == 1
        assert captured_json["owned_aquariums"][0]["name"] == "Test Aquarium"

        # Verify fish data
        assert len(captured_json["fish"]) == 1
        assert captured_json["fish"][0]["custom_name"] == "Goldie"

        # Verify streak data
        assert captured_json["streak"] is not None
        assert captured_json["streak"]["current_streak"] == 10

        # Verify achievements
        assert len(captured_json["achievements"]) == 1
        assert captured_json["achievements"][0]["achievement_type"] == "first_feeding"

        # Verify analytics events
        assert len(captured_json["analytics_events"]) >= 1


@pytest.mark.asyncio(loop_scope="session")
class TestDeleteUserData:
    """Tests for delete_user_data function."""

    async def test_delete_user_data_user_not_found(self, async_session: AsyncSession):
        """Test that delete raises UserNotFoundError for non-existent user."""
        fake_user_id = uuid4()

        with pytest.raises(UserNotFoundError) as exc_info:
            await delete_user_data(async_session, fake_user_id)

        assert exc_info.value.status_code == 404

    async def test_delete_user_data_removes_user(self, async_session: AsyncSession):
        """Test that delete removes user from database."""
        user = await create_user_with_data(async_session)
        user_id = user.id

        await delete_user_data(async_session, user_id)

        # Verify user is deleted
        result = await async_session.execute(
            select(User).where(User.id == user_id)
        )
        assert result.scalar_one_or_none() is None

    async def test_delete_user_data_removes_related_data(
        self, async_session: AsyncSession
    ):
        """Test that delete removes all related data."""
        user = await create_user_with_data(async_session)
        user_id = user.id

        await delete_user_data(async_session, user_id)

        # Verify streak is deleted
        result = await async_session.execute(
            select(Streak).where(Streak.user_id == user_id)
        )
        assert result.scalar_one_or_none() is None

        # Verify achievements are deleted
        result = await async_session.execute(
            select(Achievement).where(Achievement.user_id == user_id)
        )
        assert len(result.scalars().all()) == 0

        # Verify analytics events are deleted
        result = await async_session.execute(
            select(AnalyticsEvent).where(AnalyticsEvent.user_id == user_id)
        )
        assert len(result.scalars().all()) == 0

    async def test_delete_user_data_removes_orphan_aquariums(
        self, async_session: AsyncSession
    ):
        """Test that delete removes orphan aquariums with no other members."""
        user = await create_user_with_data(async_session)
        user_id = user.id

        # Get aquarium ID before deletion
        result = await async_session.execute(
            select(Aquarium).where(Aquarium.owner_id == user_id)
        )
        aquarium = result.scalar_one()
        aquarium_id = aquarium.id

        await delete_user_data(async_session, user_id)

        # Verify aquarium is deleted
        result = await async_session.execute(
            select(Aquarium).where(Aquarium.id == aquarium_id)
        )
        assert result.scalar_one_or_none() is None

    async def test_delete_user_data_deletes_fish(self, async_session: AsyncSession):
        """Test that delete removes fish from owned aquariums."""
        user = await create_user_with_data(async_session)
        user_id = user.id

        # Get aquarium ID before deletion
        result = await async_session.execute(
            select(Aquarium).where(Aquarium.owner_id == user_id)
        )
        aquarium = result.scalar_one()
        aquarium_id = aquarium.id

        await delete_user_data(async_session, user_id)

        # Verify fish is deleted
        result = await async_session.execute(
            select(Fish).where(Fish.aquarium_id == aquarium_id)
        )
        assert len(result.scalars().all()) == 0

    async def test_delete_user_data_fk_constraints_not_violated(
        self, async_session: AsyncSession
    ):
        """Test that deletion order respects FK constraints."""
        user = await create_user_with_data(async_session)

        # Should not raise any FK constraint errors
        await delete_user_data(async_session, user.id)

    async def test_repeated_delete_raises_user_not_found(
        self, async_session: AsyncSession
    ):
        """Test that second delete raises UserNotFoundError."""
        user = await create_user_with_data(async_session)
        user_id = user.id

        # First delete
        await delete_user_data(async_session, user_id)

        # Second delete should raise
        with pytest.raises(UserNotFoundError):
            await delete_user_data(async_session, user_id)
