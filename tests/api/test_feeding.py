"""E2E tests for feeding schedule and events API endpoints."""

import uuid

import pytest
from httpx import AsyncClient

# Pre-seeded test species IDs from conftest.py
TEST_SPECIES_GUPPY = "test-guppy"  # feeding_frequency=2
TEST_SPECIES_HUNGRY = "test-hungry"  # feeding_frequency=3


async def register_and_login(client: AsyncClient, email: str) -> dict:
    """Helper to register and login a user, returns tokens."""
    await client.post(
        "/auth/register",
        json={"email": email, "password": "SecurePass123"},
    )
    response = await client.post(
        "/auth/login",
        json={"email": email, "password": "SecurePass123"},
    )
    return response.json()


def auth_headers(tokens: dict) -> dict:
    """Helper to create auth headers."""
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def create_aquarium(client: AsyncClient, tokens: dict, name: str = "Test Tank") -> str:
    """Helper to create an aquarium and return its ID."""
    response = await client.post(
        "/aquariums",
        json={"name": name},
        headers=auth_headers(tokens),
    )
    return response.json()["id"]


@pytest.mark.asyncio(loop_scope="session")
class TestGetSchedule:
    """Tests for GET /aquariums/{id}/schedule endpoint."""

    async def test_get_schedule_without_auth_returns_401(self, client: AsyncClient):
        """Test that getting schedule without auth returns 401."""
        random_id = str(uuid.uuid4())
        response = await client.get(f"/aquariums/{random_id}/schedule")
        assert response.status_code == 401

    async def test_get_schedule_returns_null_when_not_set(self, client: AsyncClient):
        """Test that new aquarium has no schedule."""
        email = f"getsched-null-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.get(
            f"/aquariums/{aquarium_id}/schedule",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        assert response.json() is None

    async def test_get_schedule_returns_schedule_after_generate(self, client: AsyncClient):
        """Test that schedule is returned after generation."""
        email = f"getsched-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Generate schedule
        await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens),
        )

        response = await client.get(
            f"/aquariums/{aquarium_id}/schedule",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data is not None
        assert "times_per_day" in data
        assert "scheduled_times" in data

    async def test_get_schedule_other_user_aquarium_returns_403(self, client: AsyncClient):
        """Test that getting schedule of other user's aquarium returns 403."""
        email1 = f"getsched-owner-{uuid.uuid4()}@example.com"
        email2 = f"getsched-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        aquarium_id = await create_aquarium(client, tokens1)

        response = await client.get(
            f"/aquariums/{aquarium_id}/schedule",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
class TestGenerateSchedule:
    """Tests for POST /aquariums/{id}/schedule/generate endpoint."""

    async def test_generate_schedule_creates_schedule(self, client: AsyncClient):
        """Test that generate creates a schedule."""
        email = f"gensched-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "times_per_day" in data
        assert "scheduled_times" in data
        assert "food_type" in data

    async def test_generate_schedule_considers_fish_species(self, client: AsyncClient):
        """Test that schedule generation considers fish species frequency."""
        email = f"gensched-fish-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Add fish with high feeding frequency (pre-seeded species with frequency=3)
        await client.post(
            f"/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_HUNGRY},
            headers=auth_headers(tokens),
        )

        response = await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["times_per_day"] == 3
        assert len(data["scheduled_times"]) == 3

    async def test_generate_schedule_other_user_aquarium_returns_403(
        self, client: AsyncClient
    ):
        """Test that generating schedule for other user's aquarium returns 403."""
        email1 = f"gensched-owner-{uuid.uuid4()}@example.com"
        email2 = f"gensched-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        aquarium_id = await create_aquarium(client, tokens1)

        response = await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
class TestUpdateSchedule:
    """Tests for PUT /aquariums/{id}/schedule endpoint."""

    async def test_update_schedule_changes_values(self, client: AsyncClient):
        """Test that update changes schedule values."""
        email = f"updsched-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Generate schedule first
        await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens),
        )

        # Update schedule
        response = await client.put(
            f"/aquariums/{aquarium_id}/schedule",
            json={
                "times_per_day": 1,
                "scheduled_times": ["09:00"],
                "food_type": "pellets",
            },
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["times_per_day"] == 1
        assert data["scheduled_times"] == ["09:00"]
        assert data["food_type"] == "pellets"

    async def test_update_schedule_without_schedule_returns_404(self, client: AsyncClient):
        """Test that updating non-existent schedule returns 404."""
        email = f"updsched-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.put(
            f"/aquariums/{aquarium_id}/schedule",
            json={"food_type": "pellets"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestGetEvents:
    """Tests for GET /aquariums/{id}/events endpoint."""

    async def test_get_events_returns_list(self, client: AsyncClient):
        """Test that get events returns a list."""
        email = f"getevents-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Generate schedule to create events
        await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens),
        )

        response = await client.get(
            f"/aquariums/{aquarium_id}/events",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    async def test_get_events_without_auth_returns_401(self, client: AsyncClient):
        """Test that getting events without auth returns 401."""
        random_id = str(uuid.uuid4())
        response = await client.get(f"/aquariums/{random_id}/events")
        assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
class TestGetTodayEvents:
    """Tests for GET /aquariums/{id}/events/today endpoint."""

    async def test_get_today_events_returns_structure(self, client: AsyncClient):
        """Test that today events returns expected structure."""
        email = f"gettoday-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Generate schedule to create events
        await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens),
        )

        response = await client.get(
            f"/aquariums/{aquarium_id}/events/today",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert "events" in data
        assert "next_feeding" in data
        assert isinstance(data["events"], list)

    async def test_get_today_events_without_auth_returns_401(self, client: AsyncClient):
        """Test that getting today's events without auth returns 401."""
        random_id = str(uuid.uuid4())
        response = await client.get(f"/aquariums/{random_id}/events/today")
        assert response.status_code == 401

    async def test_get_today_events_other_user_returns_403(self, client: AsyncClient):
        """Test that getting today's events for other user's aquarium returns 403."""
        email1 = f"gettoday-owner-{uuid.uuid4()}@example.com"
        email2 = f"gettoday-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        aquarium_id = await create_aquarium(client, tokens1)

        response = await client.get(
            f"/aquariums/{aquarium_id}/events/today",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
class TestMarkAsFed:
    """Tests for POST /aquariums/{id}/events/{event_id}/fed endpoint."""

    async def test_mark_as_fed_updates_status(self, client: AsyncClient):
        """Test that marking as fed updates event status."""
        email = f"markfed-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Generate schedule to create events
        await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens),
        )

        # Get today's events
        events_response = await client.get(
            f"/aquariums/{aquarium_id}/events/today",
            headers=auth_headers(tokens),
        )
        events = events_response.json()["events"]

        if events:
            event_id = events[0]["id"]

            response = await client.post(
                f"/aquariums/{aquarium_id}/events/{event_id}/fed",
                headers=auth_headers(tokens),
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "completed"
            assert data["completed_at"] is not None
            assert data["completed_by"] is not None

    async def test_mark_as_fed_without_auth_returns_401(self, client: AsyncClient):
        """Test that marking as fed without auth returns 401."""
        random_aq_id = str(uuid.uuid4())
        random_event_id = str(uuid.uuid4())
        response = await client.post(f"/aquariums/{random_aq_id}/events/{random_event_id}/fed")
        assert response.status_code == 401

    async def test_mark_as_fed_nonexistent_event_returns_404(self, client: AsyncClient):
        """Test that marking non-existent event returns 404."""
        email = f"markfed-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)
        random_event_id = str(uuid.uuid4())

        response = await client.post(
            f"/aquariums/{aquarium_id}/events/{random_event_id}/fed",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestMarkAsMissed:
    """Tests for POST /aquariums/{id}/events/{event_id}/missed endpoint."""

    async def test_mark_as_missed_updates_status(self, client: AsyncClient):
        """Test that marking as missed updates event status."""
        email = f"markmissed-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Generate schedule to create events
        await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens),
        )

        # Get today's events
        events_response = await client.get(
            f"/aquariums/{aquarium_id}/events/today",
            headers=auth_headers(tokens),
        )
        events = events_response.json()["events"]

        if events:
            event_id = events[0]["id"]

            response = await client.post(
                f"/aquariums/{aquarium_id}/events/{event_id}/missed",
                headers=auth_headers(tokens),
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "missed"

    async def test_mark_as_missed_without_auth_returns_401(self, client: AsyncClient):
        """Test that marking as missed without auth returns 401."""
        random_aq_id = str(uuid.uuid4())
        random_event_id = str(uuid.uuid4())
        response = await client.post(
            f"/aquariums/{random_aq_id}/events/{random_event_id}/missed"
        )
        assert response.status_code == 401

    async def test_mark_as_missed_nonexistent_event_returns_404(self, client: AsyncClient):
        """Test that marking non-existent event as missed returns 404."""
        email = f"markmissed-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)
        random_event_id = str(uuid.uuid4())

        response = await client.post(
            f"/aquariums/{aquarium_id}/events/{random_event_id}/missed",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404

    async def test_mark_as_missed_other_user_returns_403(self, client: AsyncClient):
        """Test that marking other user's event as missed returns 403."""
        email1 = f"markmissed-owner-{uuid.uuid4()}@example.com"
        email2 = f"markmissed-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        aquarium_id = await create_aquarium(client, tokens1)

        # Generate schedule
        await client.post(
            f"/aquariums/{aquarium_id}/schedule/generate",
            headers=auth_headers(tokens1),
        )

        # Get events
        events_response = await client.get(
            f"/aquariums/{aquarium_id}/events/today",
            headers=auth_headers(tokens1),
        )
        events = events_response.json()["events"]

        if events:
            event_id = events[0]["id"]

            # User 2 tries to mark as missed
            response = await client.post(
                f"/aquariums/{aquarium_id}/events/{event_id}/missed",
                headers=auth_headers(tokens2),
            )

            assert response.status_code == 403
