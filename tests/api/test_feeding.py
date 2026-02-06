"""E2E tests for feeding schedule and feeding log API endpoints."""

import uuid
from datetime import date, datetime

import pytest
from httpx import AsyncClient

# Pre-seeded test species IDs from conftest.py
TEST_SPECIES_GUPPY = "test-guppy"  # feeding_frequency=2
TEST_SPECIES_HUNGRY = "test-hungry"  # feeding_frequency=3


async def register_and_login(client: AsyncClient, email: str) -> dict:
    """Helper to register and login a user, returns tokens."""
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "SecurePass123"},
    )
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "SecurePass123"},
    )
    return response.json()


def auth_headers(tokens: dict) -> dict:
    """Helper to create auth headers."""
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def create_aquarium(client: AsyncClient, tokens: dict, name: str = "Test Tank") -> str:
    """Helper to create an aquarium and return its ID."""
    response = await client.post(
        "/api/v1/aquariums",
        json={"name": name},
        headers=auth_headers(tokens),
    )
    return response.json()["id"]


async def add_fish_and_generate(
    client: AsyncClient, tokens: dict, aquarium_id: str, species_id: str = TEST_SPECIES_GUPPY
) -> list[dict]:
    """Helper: add fish, generate schedules, return schedule list."""
    await client.post(
        f"/api/v1/aquariums/{aquarium_id}/fish",
        json={"species_id": species_id},
        headers=auth_headers(tokens),
    )
    response = await client.post(
        f"/api/v1/aquariums/{aquarium_id}/schedules/generate",
        headers=auth_headers(tokens),
    )
    return response.json()


@pytest.mark.asyncio(loop_scope="session")
class TestListSchedules:
    """Tests for GET /aquariums/{id}/schedules endpoint."""

    async def test_list_schedules_without_auth_returns_401(self, client: AsyncClient):
        random_id = str(uuid.uuid4())
        response = await client.get(f"/api/v1/aquariums/{random_id}/schedules")
        assert response.status_code == 401

    async def test_list_schedules_returns_empty_list(self, client: AsyncClient):
        email = f"listsched-empty-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/schedules",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_schedules_after_generate(self, client: AsyncClient):
        email = f"listsched-gen-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        await add_fish_and_generate(client, tokens, aquarium_id)

        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/schedules",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert "fish_id" in data[0]
        assert "time" in data[0]
        assert "interval_days" in data[0]

    async def test_list_schedules_active_filter(self, client: AsyncClient):
        email = f"listsched-filter-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        schedules = await add_fish_and_generate(client, tokens, aquarium_id)

        # Deactivate first schedule
        schedule_id = schedules[0]["id"]
        await client.patch(
            f"/api/v1/aquariums/{aquarium_id}/schedules/{schedule_id}",
            json={"active": False},
            headers=auth_headers(tokens),
        )

        # Filter active=true
        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/schedules?active=true",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 200
        active_schedules = response.json()
        assert all(s["active"] for s in active_schedules)

    async def test_list_schedules_other_user_returns_403(self, client: AsyncClient):
        email1 = f"listsched-owner-{uuid.uuid4()}@example.com"
        email2 = f"listsched-other-{uuid.uuid4()}@example.com"
        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)
        aquarium_id = await create_aquarium(client, tokens1)

        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/schedules",
            headers=auth_headers(tokens2),
        )
        assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
class TestGenerateSchedules:
    """Tests for POST /aquariums/{id}/schedules/generate endpoint."""

    async def test_generate_creates_schedules(self, client: AsyncClient):
        email = f"gensched-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        schedules = await add_fish_and_generate(client, tokens, aquarium_id)

        assert isinstance(schedules, list)
        assert len(schedules) >= 1
        assert "id" in schedules[0]
        assert "fish_id" in schedules[0]
        assert "food_type" in schedules[0]

    async def test_generate_considers_fish_species(self, client: AsyncClient):
        email = f"gensched-fish-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        schedules = await add_fish_and_generate(client, tokens, aquarium_id, TEST_SPECIES_HUNGRY)

        # Species frequency=3 should produce 3 schedules for the fish
        assert len(schedules) == 3

    async def test_generate_other_user_aquarium_returns_403(self, client: AsyncClient):
        email1 = f"gensched-owner-{uuid.uuid4()}@example.com"
        email2 = f"gensched-other-{uuid.uuid4()}@example.com"
        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)
        aquarium_id = await create_aquarium(client, tokens1)

        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/schedules/generate",
            headers=auth_headers(tokens2),
        )
        assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
class TestCreateSchedule:
    """Tests for POST /aquariums/{id}/schedules endpoint."""

    async def test_create_schedule_returns_201(self, client: AsyncClient):
        email = f"createsched-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Add fish first
        await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_GUPPY},
            headers=auth_headers(tokens),
        )
        fish_response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            headers=auth_headers(tokens),
        )
        fish_id = fish_response.json()[0]["id"]

        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/schedules",
            json={
                "fish_id": fish_id,
                "time": "10:30",
                "interval_days": 1,
                "anchor_date": str(date.today()),
                "food_type": "pellets",
            },
            headers=auth_headers(tokens),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["fish_id"] == fish_id
        assert data["time"] == "10:30"
        assert data["active"] is True


@pytest.mark.asyncio(loop_scope="session")
class TestUpdateSchedule:
    """Tests for PATCH /aquariums/{id}/schedules/{schedule_id} endpoint."""

    async def test_update_schedule_changes_values(self, client: AsyncClient):
        email = f"updsched-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        schedules = await add_fish_and_generate(client, tokens, aquarium_id)
        schedule_id = schedules[0]["id"]

        response = await client.patch(
            f"/api/v1/aquariums/{aquarium_id}/schedules/{schedule_id}",
            json={"time": "14:00", "food_type": "pellets"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["time"] == "14:00"
        assert data["food_type"] == "pellets"

    async def test_update_nonexistent_schedule_returns_404(self, client: AsyncClient):
        email = f"updsched-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)
        random_id = str(uuid.uuid4())

        response = await client.patch(
            f"/api/v1/aquariums/{aquarium_id}/schedules/{random_id}",
            json={"food_type": "pellets"},
            headers=auth_headers(tokens),
        )
        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestDeleteSchedule:
    """Tests for DELETE /aquariums/{id}/schedules/{schedule_id} endpoint."""

    async def test_delete_schedule_returns_204(self, client: AsyncClient):
        email = f"delsched-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        schedules = await add_fish_and_generate(client, tokens, aquarium_id)
        schedule_id = schedules[0]["id"]

        response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}/schedules/{schedule_id}",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 204

    async def test_delete_nonexistent_schedule_returns_404(self, client: AsyncClient):
        email = f"delsched-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)
        random_id = str(uuid.uuid4())

        response = await client.delete(
            f"/api/v1/aquariums/{aquarium_id}/schedules/{random_id}",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestFeedingLogs:
    """Tests for feeding log endpoints."""

    async def test_get_feeding_logs_without_auth_returns_401(self, client: AsyncClient):
        random_id = str(uuid.uuid4())
        response = await client.get(
            f"/api/v1/aquariums/{random_id}/feeding-logs?from=2025-01-01T00:00:00&to=2025-01-02T00:00:00"
        )
        assert response.status_code == 401

    async def test_get_feeding_logs_returns_empty_list(self, client: AsyncClient):
        email = f"getlogs-empty-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/feeding-logs?from=2025-01-01T00:00:00&to=2025-12-31T23:59:59",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 200
        assert response.json() == []

    async def test_create_feeding_log_returns_201(self, client: AsyncClient):
        email = f"createlog-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        schedules = await add_fish_and_generate(client, tokens, aquarium_id)
        schedule = schedules[0]

        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/feeding-logs",
            json={
                "schedule_id": schedule["id"],
                "fish_id": schedule["fish_id"],
                "scheduled_for": datetime.now().isoformat(),
                "action": "fed",
                "device_id": str(uuid.uuid4()),
            },
            headers=auth_headers(tokens),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["action"] == "fed"
        assert data["schedule_id"] == schedule["id"]

    async def test_duplicate_feeding_log_returns_409(self, client: AsyncClient):
        email = f"duplog-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        schedules = await add_fish_and_generate(client, tokens, aquarium_id)
        schedule = schedules[0]
        scheduled_for = "2025-06-15T09:00:00"

        log_data = {
            "schedule_id": schedule["id"],
            "fish_id": schedule["fish_id"],
            "scheduled_for": scheduled_for,
            "action": "fed",
            "device_id": str(uuid.uuid4()),
        }

        # First create succeeds
        r1 = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/feeding-logs",
            json=log_data,
            headers=auth_headers(tokens),
        )
        assert r1.status_code == 201

        # Second create returns 409
        r2 = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/feeding-logs",
            json=log_data,
            headers=auth_headers(tokens),
        )
        assert r2.status_code == 409
        data = r2.json()
        assert data["error"] == "conflict"
        assert "existing_log" in data

    async def test_create_feeding_log_other_user_returns_403(self, client: AsyncClient):
        email1 = f"createlog-owner-{uuid.uuid4()}@example.com"
        email2 = f"createlog-other-{uuid.uuid4()}@example.com"
        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)
        aquarium_id = await create_aquarium(client, tokens1)

        schedules = await add_fish_and_generate(client, tokens1, aquarium_id)
        schedule = schedules[0]

        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/feeding-logs",
            json={
                "schedule_id": schedule["id"],
                "fish_id": schedule["fish_id"],
                "scheduled_for": datetime.now().isoformat(),
                "action": "fed",
                "device_id": str(uuid.uuid4()),
            },
            headers=auth_headers(tokens2),
        )
        assert response.status_code == 403

    async def test_date_range_exceeds_366_days_returns_400(self, client: AsyncClient):
        email = f"getlogs-range-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/feeding-logs?from=2024-01-01T00:00:00&to=2025-12-31T23:59:59",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 400
