"""Integration tests for sync API endpoint."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient


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


@pytest.mark.asyncio(loop_scope="session")
class TestSyncAuthentication:
    """Tests for sync endpoint authentication."""

    async def test_sync_without_auth_returns_401(self, client: AsyncClient):
        """Test that sync without auth returns 401."""
        response = await client.post(
            "/sync",
            json={"changes": [], "last_sync_at": None},
        )
        assert response.status_code == 401

    async def test_sync_with_auth_returns_200(self, client: AsyncClient):
        """Test that sync with auth returns 200."""
        email = f"sync-auth-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/sync",
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
            "/sync",
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
            "/aquariums",
            json={"name": "Sync Test Tank"},
            headers=auth_headers(tokens),
        )

        response = await client.post(
            "/sync",
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
            "/sync",
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
            "/sync",
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
            "/sync",
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
            "/sync",
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
                "/aquariums",
                json={"name": f"Tank {i}"},
                headers=auth_headers(tokens),
            )

        # Request with page_size=2
        response = await client.post(
            "/sync",
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
            "/sync",
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
            "/sync",
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
            "/sync",
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
            "/sync",
            json={"changes": []},
            headers=auth_headers(tokens),
        )
        assert response1.status_code == 200
        etag = response1.headers.get("ETag")

        # Second sync with If-None-Match and delta sync (last_sync_at)
        # Using a recent timestamp to get delta sync with no changes
        response2 = await client.post(
            "/sync",
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
            "/aquariums",
            json={"name": "Original Name"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # Update via sync with newer timestamp
        response = await client.post(
            "/sync",
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
            "/sync",
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
            "/sync",
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
            "/aquariums",
            json={"name": "First Tank"},
            headers=auth_headers(tokens),
        )

        # Initial sync
        response1 = await client.post(
            "/sync",
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
            "/aquariums",
            json={"name": "Second Tank"},
            headers=auth_headers(tokens),
        )

        # Delta sync - should only return the new aquarium
        response2 = await client.post(
            "/sync",
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

    async def test_sync_cannot_access_other_user_aquarium(self, client: AsyncClient):
        """Test that sync cannot modify other user's aquarium."""
        email1 = f"sync-access-owner-{uuid.uuid4()}@example.com"
        email2 = f"sync-access-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # User 1 creates aquarium
        create_response = await client.post(
            "/aquariums",
            json={"name": "Owner's Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # User 2 tries to update via sync
        response = await client.post(
            "/sync",
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
