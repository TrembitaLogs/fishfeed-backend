"""Integration tests for sync API endpoint."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.species import Species


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


@pytest.mark.asyncio(loop_scope="session")
class TestSyncAuthentication:
    """Tests for sync endpoint authentication."""

    async def test_sync_without_auth_returns_401(self, client: AsyncClient):
        """Test that sync without auth returns 401."""
        response = await client.post(
            "/api/v1/sync",
            json={"changes": [], "last_sync_at": None},
        )
        assert response.status_code == 401

    async def test_sync_with_auth_returns_200(self, client: AsyncClient):
        """Test that sync with auth returns 200."""
        email = f"sync-auth-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/sync",
            json={"changes": [], "last_sync_at": None},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
class TestSyncBasicFunctionality:
    """Tests for basic sync functionality."""

    async def test_sync_empty_changes_returns_server_state(self, client: AsyncClient):
        """Test that sync with empty changes returns server state."""
        email = f"sync-empty-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/sync",
            json={"changes": []},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert "server_state" in data
        assert "conflicts" in data
        assert "sync_token" in data
        assert data["conflicts"] == []

    async def test_sync_returns_user_aquariums(self, client: AsyncClient):
        """Test that sync returns user's aquariums."""
        email = f"sync-aquariums-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium via regular API
        await client.post(
            "/api/v1/aquariums",
            json={"name": "Sync Test Tank"},
            headers=auth_headers(tokens),
        )

        response = await client.post(
            "/api/v1/sync",
            json={"changes": []},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["server_state"]["aquariums"]) == 1
        assert data["server_state"]["aquariums"][0]["name"] == "Sync Test Tank"

    async def test_sync_create_aquarium(self, client: AsyncClient):
        """Test that sync can create aquarium."""
        email = f"sync-create-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = str(uuid.uuid4())

        response = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aquarium_id,
                        "operation": "create",
                        "data": {"name": "Synced Aquarium"},
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["server_state"]["aquariums"]) == 1
        assert data["server_state"]["aquariums"][0]["name"] == "Synced Aquarium"


@pytest.mark.asyncio(loop_scope="session")
class TestSyncPagination:
    """Tests for sync pagination."""

    async def test_sync_pagination_default_page_size(self, client: AsyncClient):
        """Test that sync uses default page_size of 100."""
        email = f"sync-page-default-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/sync",
            json={"changes": []},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert "has_more" in data
        assert "next_cursor" in data
        assert data["has_more"] is False
        assert data["next_cursor"] is None

    async def test_sync_pagination_custom_page_size(self, client: AsyncClient):
        """Test that sync respects custom page_size."""
        email = f"sync-page-custom-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/sync",
            json={"changes": [], "page_size": 10},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["has_more"] is False

    async def test_sync_pagination_invalid_page_size_returns_422(
        self, client: AsyncClient
    ):
        """Test that invalid page_size returns 422."""
        email = f"sync-page-invalid-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # page_size > 500
        response = await client.post(
            "/api/v1/sync",
            json={"changes": [], "page_size": 1000},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 422

    async def test_sync_pagination_with_many_items(self, client: AsyncClient):
        """Test pagination with multiple items."""
        email = f"sync-page-many-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create 5 aquariums
        for i in range(5):
            await client.post(
                "/api/v1/aquariums",
                json={"name": f"Tank {i}"},
                headers=auth_headers(tokens),
            )

        # Request with page_size=2
        response = await client.post(
            "/api/v1/sync",
            json={"changes": [], "page_size": 2},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["has_more"] is True
        assert data["next_cursor"] is not None
        assert len(data["server_state"]["aquariums"]) == 2

        # Fetch next page
        response2 = await client.post(
            "/api/v1/sync",
            json={"changes": [], "page_size": 2, "cursor": data["next_cursor"]},
            headers=auth_headers(tokens),
        )

        assert response2.status_code == 200
        data2 = response2.json()
        assert len(data2["server_state"]["aquariums"]) == 2


@pytest.mark.asyncio(loop_scope="session")
class TestSyncETag:
    """Tests for sync ETag cache validation."""

    async def test_sync_returns_etag_header(self, client: AsyncClient):
        """Test that sync response includes ETag header."""
        email = f"sync-etag-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/sync",
            json={"changes": []},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        assert "ETag" in response.headers

    async def test_sync_returns_correlation_id_header(self, client: AsyncClient):
        """Test that sync response includes X-Correlation-ID header."""
        email = f"sync-corr-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/sync",
            json={"changes": []},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        assert "X-Correlation-ID" in response.headers

    async def test_sync_304_when_no_changes(self, client: AsyncClient):
        """Test that sync returns 304 when If-None-Match and no changes."""
        email = f"sync-304-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # First sync to get ETag
        response1 = await client.post(
            "/api/v1/sync",
            json={"changes": []},
            headers=auth_headers(tokens),
        )
        assert response1.status_code == 200
        etag = response1.headers.get("ETag")

        # Second sync with If-None-Match and delta sync (last_sync_at)
        # Using a recent timestamp to get delta sync with no changes
        response2 = await client.post(
            "/api/v1/sync",
            json={
                "changes": [],
                "last_sync_at": datetime.now(UTC).isoformat(),
            },
            headers={
                **auth_headers(tokens),
                "If-None-Match": etag,
            },
        )

        # Should return 304 Not Modified (no new changes)
        assert response2.status_code == 304


@pytest.mark.asyncio(loop_scope="session")
class TestSyncConflictResolution:
    """Tests for sync conflict resolution."""

    async def test_sync_update_with_newer_timestamp_wins(self, client: AsyncClient):
        """Test that update with newer timestamp wins (last-write-wins)."""
        email = f"sync-lww-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Original Name"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # Update via sync with newer timestamp
        response = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aquarium_id,
                        "operation": "update",
                        "data": {"name": "Updated Name"},
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        # Should have no conflicts (client wins with newer timestamp)
        assert len(data["conflicts"]) == 0
        # Verify the update was applied
        aquarium = next(
            (a for a in data["server_state"]["aquariums"] if a["id"] == aquarium_id),
            None,
        )
        assert aquarium is not None
        assert aquarium["name"] == "Updated Name"


@pytest.mark.asyncio(loop_scope="session")
class TestSyncBatchProcessing:
    """Tests for sync batch processing."""

    async def test_sync_processes_multiple_changes(self, client: AsyncClient):
        """Test that sync processes multiple changes in one request."""
        email = f"sync-batch-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        now = datetime.now(UTC).isoformat()

        # Create multiple aquariums in one sync
        changes = []
        aquarium_ids = []
        for i in range(5):
            aq_id = str(uuid.uuid4())
            aquarium_ids.append(aq_id)
            changes.append(
                {
                    "entity_type": "aquarium",
                    "entity_id": aq_id,
                    "operation": "create",
                    "data": {"name": f"Batch Tank {i}"},
                    "client_updated_at": now,
                }
            )

        response = await client.post(
            "/api/v1/sync",
            json={"changes": changes},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["server_state"]["aquariums"]) == 5

    async def test_sync_large_batch_over_100_changes(self, client: AsyncClient):
        """Test that sync handles batches with 100+ changes."""
        email = f"sync-large-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        now = datetime.now(UTC).isoformat()

        # Create 150 aquariums in one sync
        changes = []
        for i in range(150):
            changes.append(
                {
                    "entity_type": "aquarium",
                    "entity_id": str(uuid.uuid4()),
                    "operation": "create",
                    "data": {"name": f"Large Batch Tank {i}"},
                    "client_updated_at": now,
                }
            )

        response = await client.post(
            "/api/v1/sync",
            json={"changes": changes, "page_size": 500},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        # All 150 aquariums should be created
        assert len(data["server_state"]["aquariums"]) == 150


@pytest.mark.asyncio(loop_scope="session")
class TestSyncDeltaSync:
    """Tests for delta sync functionality."""

    async def test_delta_sync_returns_only_new_changes(self, client: AsyncClient):
        """Test that delta sync returns only changes after last_sync_at."""
        from datetime import timedelta

        email = f"sync-delta-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Record timestamp BEFORE creating first aquarium
        initial_time = datetime.now(UTC) - timedelta(seconds=1)

        # Create first aquarium
        await client.post(
            "/api/v1/aquariums",
            json={"name": "First Tank"},
            headers=auth_headers(tokens),
        )

        # Initial sync
        response1 = await client.post(
            "/api/v1/sync",
            json={"changes": []},
            headers=auth_headers(tokens),
        )
        assert response1.status_code == 200
        assert len(response1.json()["server_state"]["aquariums"]) == 1

        # Record timestamp AFTER first aquarium but BEFORE second
        # Using the timestamp from the first aquarium response
        first_aquarium = response1.json()["server_state"]["aquariums"][0]
        sync_time = datetime.fromisoformat(
            first_aquarium["updated_at"].replace("Z", "+00:00")
        ) + timedelta(milliseconds=1)

        # Create second aquarium
        await client.post(
            "/api/v1/aquariums",
            json={"name": "Second Tank"},
            headers=auth_headers(tokens),
        )

        # Delta sync - should only return the new aquarium
        response2 = await client.post(
            "/api/v1/sync",
            json={"changes": [], "last_sync_at": sync_time.isoformat()},
            headers=auth_headers(tokens),
        )

        assert response2.status_code == 200
        data = response2.json()
        # Should only have the new aquarium
        assert len(data["server_state"]["aquariums"]) == 1
        assert data["server_state"]["aquariums"][0]["name"] == "Second Tank"


@pytest.mark.asyncio(loop_scope="session")
class TestSyncAccessControl:
    """Tests for sync access control."""

    async def test_sync_cannot_access_other_user_aquarium(
        self, client: AsyncClient
    ):
        """Test that sync cannot modify other user's aquarium."""
        email1 = f"sync-access-owner-{uuid.uuid4()}@example.com"
        email2 = f"sync-access-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # User 1 creates aquarium
        create_response = await client.post(
            "/api/v1/aquariums",
            json={"name": "Owner's Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # User 2 tries to update via sync
        response = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "aquarium",
                        "entity_id": aquarium_id,
                        "operation": "update",
                        "data": {"name": "Hacked Name"},
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens2),
        )

        # Should return 403 Access Denied
        assert response.status_code == 403


async def _setup_aquarium_with_schedule(
    client: AsyncClient, tokens: dict
) -> tuple[str, str, str]:
    """Helper: create aquarium, add fish, create schedule via sync.

    Uses two sync calls because access validation runs BEFORE changes are
    applied — so the aquarium must exist before fish/schedule can reference it.
    Uses ``test-betta`` species which is never deleted by other test modules.

    Returns (aquarium_id, fish_id, schedule_id).
    """
    now = datetime.now(UTC)
    aquarium_id = str(uuid.uuid4())
    fish_id = str(uuid.uuid4())
    schedule_id = str(uuid.uuid4())
    hdrs = auth_headers(tokens)

    # Step 1: Create aquarium (adds user as owner/member)
    resp1 = await client.post(
        "/api/v1/sync",
        json={
            "changes": [
                {
                    "entity_type": "aquarium",
                    "entity_id": aquarium_id,
                    "operation": "create",
                    "data": {"name": f"Sync FL Tank {uuid.uuid4().hex[:6]}"},
                    "client_updated_at": now.isoformat(),
                }
            ]
        },
        headers=hdrs,
    )
    assert resp1.status_code == 200, (
        f"Aquarium setup failed: {resp1.status_code} {resp1.text}"
    )

    # Step 2: Create fish + schedule (aquarium now exists in DB)
    resp2 = await client.post(
        "/api/v1/sync",
        json={
            "changes": [
                {
                    "entity_type": "fish",
                    "entity_id": fish_id,
                    "operation": "create",
                    "data": {
                        "aquarium_id": aquarium_id,
                        "species_id": "test-betta",
                        "quantity": 1,
                    },
                    "client_updated_at": now.isoformat(),
                },
                {
                    "entity_type": "schedule",
                    "entity_id": schedule_id,
                    "operation": "create",
                    "data": {
                        "aquarium_id": aquarium_id,
                        "fish_id": fish_id,
                        "time": "09:00",
                        "interval_days": 1,
                        "anchor_date": now.strftime("%Y-%m-%d"),
                        "food_type": "flakes",
                        "active": True,
                    },
                    "client_updated_at": now.isoformat(),
                },
            ]
        },
        headers=hdrs,
    )
    assert resp2.status_code == 200, (
        f"Fish/schedule setup failed: {resp2.status_code} {resp2.text}"
    )

    return aquarium_id, fish_id, schedule_id


@pytest_asyncio.fixture(loop_scope="session")
async def ensure_species(async_session: AsyncSession) -> None:
    """Ensure test species exist — other modules may delete them."""
    result = await async_session.execute(
        select(Species).where(Species.id == "test-betta")
    )
    if result.scalar_one_or_none() is None:
        async_session.add(
            Species(
                id="test-betta",
                common_name="Test Betta",
                scientific_name="Betta splendens",
                food_types=["pellets", "live"],
                feeding_frequency=2,
                care_level="beginner",
                water_type="freshwater",
            )
        )
        await async_session.commit()


@pytest.mark.usefixtures("ensure_species")
@pytest.mark.asyncio(loop_scope="session")
class TestSyncFeedingLogs:
    """Tests for feeding_log sync via POST /sync."""

    async def test_sync_create_feeding_log(self, client: AsyncClient):
        """Test creating a feeding log through sync endpoint."""
        email = f"sync-fl-create-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id, fish_id, schedule_id = await _setup_aquarium_with_schedule(
            client, tokens
        )

        log_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        scheduled_for = now.replace(tzinfo=None).isoformat()

        response = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "feeding_log",
                        "entity_id": log_id,
                        "operation": "create",
                        "data": {
                            "schedule_id": schedule_id,
                            "fish_id": fish_id,
                            "aquarium_id": aquarium_id,
                            "scheduled_for": scheduled_for,
                            "action": "fed",
                            "device_id": str(uuid.uuid4()),
                            "acted_at": now.isoformat(),
                        },
                        "client_updated_at": now.isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["conflicts"] == []
        log_ids = [fl["id"] for fl in data["server_state"]["feeding_logs"]]
        assert log_id in log_ids

    async def test_sync_feeding_log_duplicate_conflict(self, client: AsyncClient):
        """Test that duplicate feeding_log returns first-write-wins conflict."""
        email = f"sync-fl-dup-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id, fish_id, schedule_id = await _setup_aquarium_with_schedule(
            client, tokens
        )

        now = datetime.now(UTC)
        scheduled_for = now.replace(tzinfo=None).isoformat()

        # First log — should succeed
        first_id = str(uuid.uuid4())
        resp1 = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "feeding_log",
                        "entity_id": first_id,
                        "operation": "create",
                        "data": {
                            "schedule_id": schedule_id,
                            "fish_id": fish_id,
                            "aquarium_id": aquarium_id,
                            "scheduled_for": scheduled_for,
                            "action": "fed",
                            "device_id": str(uuid.uuid4()),
                            "acted_at": now.isoformat(),
                        },
                        "client_updated_at": now.isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens),
        )
        assert resp1.status_code == 200
        assert resp1.json()["conflicts"] == []

        # Second log with SAME (schedule_id, scheduled_for) — should conflict
        second_id = str(uuid.uuid4())
        resp2 = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "feeding_log",
                        "entity_id": second_id,
                        "operation": "create",
                        "data": {
                            "schedule_id": schedule_id,
                            "fish_id": fish_id,
                            "aquarium_id": aquarium_id,
                            "scheduled_for": scheduled_for,
                            "action": "fed",
                            "device_id": str(uuid.uuid4()),
                            "acted_at": now.isoformat(),
                        },
                        "client_updated_at": now.isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens),
        )

        assert resp2.status_code == 200
        conflicts = resp2.json()["conflicts"]
        assert len(conflicts) == 1
        assert conflicts[0]["entity_type"] == "feeding_log"
        assert conflicts[0]["resolution"] == "server_wins"

    async def test_sync_delta_returns_new_feeding_logs(self, client: AsyncClient):
        """Test that delta sync returns feeding_logs created after last_sync_at."""
        email = f"sync-fl-delta-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id, fish_id, schedule_id = await _setup_aquarium_with_schedule(
            client, tokens
        )

        # Record time BEFORE creating a log
        t_before = datetime.now(UTC)

        # Create feeding log via sync
        log_id = str(uuid.uuid4())
        scheduled_for = datetime.now(UTC).replace(tzinfo=None).isoformat()
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "feeding_log",
                        "entity_id": log_id,
                        "operation": "create",
                        "data": {
                            "schedule_id": schedule_id,
                            "fish_id": fish_id,
                            "aquarium_id": aquarium_id,
                            "scheduled_for": scheduled_for,
                            "action": "fed",
                            "device_id": str(uuid.uuid4()),
                        },
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens),
        )

        # Delta sync with timestamp BEFORE the log — should include it
        resp_before = await client.post(
            "/api/v1/sync",
            json={
                "changes": [],
                "last_sync_at": (t_before - timedelta(seconds=1)).isoformat(),
            },
            headers=auth_headers(tokens),
        )
        assert resp_before.status_code == 200
        log_ids_before = [
            fl["id"] for fl in resp_before.json()["server_state"]["feeding_logs"]
        ]
        assert log_id in log_ids_before

        # Delta sync with timestamp AFTER the log — should NOT include it
        resp_after = await client.post(
            "/api/v1/sync",
            json={
                "changes": [],
                "last_sync_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
            },
            headers=auth_headers(tokens),
        )
        assert resp_after.status_code in (200, 304)
        if resp_after.status_code == 200:
            log_ids_after = [
                fl["id"]
                for fl in resp_after.json()["server_state"]["feeding_logs"]
            ]
            assert log_id not in log_ids_after

    async def test_sync_feeding_log_update_ignored(self, client: AsyncClient):
        """Test that update operation on feeding_log is ignored (immutable)."""
        email = f"sync-fl-upd-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id, fish_id, schedule_id = await _setup_aquarium_with_schedule(
            client, tokens
        )

        now = datetime.now(UTC)
        log_id = str(uuid.uuid4())
        scheduled_for = now.replace(tzinfo=None).isoformat()

        # Create the log first
        await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "feeding_log",
                        "entity_id": log_id,
                        "operation": "create",
                        "data": {
                            "schedule_id": schedule_id,
                            "fish_id": fish_id,
                            "aquarium_id": aquarium_id,
                            "scheduled_for": scheduled_for,
                            "action": "fed",
                            "device_id": str(uuid.uuid4()),
                        },
                        "client_updated_at": now.isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens),
        )

        # Try to update it — should be ignored
        resp = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "feeding_log",
                        "entity_id": log_id,
                        "operation": "update",
                        "data": {"action": "skipped", "notes": "changed"},
                        "client_updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            headers=auth_headers(tokens),
        )
        assert resp.status_code == 200
        # No conflicts, update was silently ignored
        assert resp.json()["conflicts"] == []

        # Verify original log is unchanged
        logs = resp.json()["server_state"]["feeding_logs"]
        original = next((fl for fl in logs if fl["id"] == log_id), None)
        assert original is not None
        assert original["action"] == "fed"

    async def test_sync_schedule_last_write_wins(self, client: AsyncClient):
        """Test last-write-wins for schedule sync updates."""
        email = f"sync-sched-lww-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id, fish_id, schedule_id = await _setup_aquarium_with_schedule(
            client, tokens
        )

        # Update with NEWER timestamp — should succeed
        future_ts = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        resp1 = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "schedule",
                        "entity_id": schedule_id,
                        "operation": "update",
                        "data": {"food_type": "pellets"},
                        "client_updated_at": future_ts,
                    }
                ]
            },
            headers=auth_headers(tokens),
        )
        assert resp1.status_code == 200
        assert resp1.json()["conflicts"] == []
        schedule = next(
            (
                s
                for s in resp1.json()["server_state"]["schedules"]
                if s["id"] == schedule_id
            ),
            None,
        )
        assert schedule is not None
        assert schedule["food_type"] == "pellets"

        # Update with OLDER timestamp — should conflict (server wins)
        past_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        resp2 = await client.post(
            "/api/v1/sync",
            json={
                "changes": [
                    {
                        "entity_type": "schedule",
                        "entity_id": schedule_id,
                        "operation": "update",
                        "data": {"food_type": "worms"},
                        "client_updated_at": past_ts,
                    }
                ]
            },
            headers=auth_headers(tokens),
        )
        assert resp2.status_code == 200
        conflicts = resp2.json()["conflicts"]
        assert len(conflicts) == 1
        assert conflicts[0]["entity_type"] == "schedule"
        assert conflicts[0]["resolution"] == "server_wins"
        # food_type should still be "pellets"
        schedule2 = next(
            (
                s
                for s in resp2.json()["server_state"]["schedules"]
                if s["id"] == schedule_id
            ),
            None,
        )
        assert schedule2 is not None
        assert schedule2["food_type"] == "pellets"

