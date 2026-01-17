"""E2E tests for aquarium API endpoints."""

import uuid

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
class TestListAquariums:
    """Tests for GET /aquariums endpoint."""

    async def test_list_aquariums_without_auth_returns_401(self, client: AsyncClient):
        """Test that listing aquariums without auth returns 401."""
        response = await client.get("/aquariums")
        assert response.status_code == 401

    async def test_list_aquariums_returns_empty_list(self, client: AsyncClient):
        """Test that new user has empty aquarium list."""
        email = f"listaq-empty-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.get("/aquariums", headers=auth_headers(tokens))

        assert response.status_code == 200
        assert response.json() == []

    async def test_list_aquariums_returns_user_aquariums(self, client: AsyncClient):
        """Test that list returns user's aquariums."""
        email = f"listaq-owned-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquariums
        await client.post(
            "/aquariums",
            json={"name": "My Tank 1"},
            headers=auth_headers(tokens),
        )
        await client.post(
            "/aquariums",
            json={"name": "My Tank 2"},
            headers=auth_headers(tokens),
        )

        response = await client.get("/aquariums", headers=auth_headers(tokens))

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        names = [a["name"] for a in data]
        assert "My Tank 1" in names
        assert "My Tank 2" in names


@pytest.mark.asyncio(loop_scope="session")
class TestCreateAquarium:
    """Tests for POST /aquariums endpoint."""

    async def test_create_aquarium_returns_201(self, client: AsyncClient):
        """Test that creating aquarium returns 201."""
        email = f"createaq-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/aquariums",
            json={"name": "My New Aquarium"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My New Aquarium"
        assert "id" in data
        assert "owner_id" in data
        assert "created_at" in data

    async def test_create_aquarium_without_auth_returns_401(self, client: AsyncClient):
        """Test that creating aquarium without auth returns 401."""
        response = await client.post(
            "/aquariums",
            json={"name": "Unauthorized Tank"},
        )
        assert response.status_code == 401

    async def test_create_aquarium_invalid_name_returns_422(self, client: AsyncClient):
        """Test that creating aquarium with invalid name returns 422."""
        email = f"createaq-invalid-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/aquariums",
            json={"name": ""},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestGetAquarium:
    """Tests for GET /aquariums/{id} endpoint."""

    async def test_get_aquarium_returns_details_with_fish_and_schedule(
        self, client: AsyncClient
    ):
        """Test that get aquarium returns fish and schedule."""
        email = f"getaq-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/aquariums",
            json={"name": "Detail Tank"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        response = await client.get(
            f"/aquariums/{aquarium_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Detail Tank"
        assert "fish" in data
        assert data["fish"] == []
        assert "schedule" in data

    async def test_get_nonexistent_aquarium_returns_404(self, client: AsyncClient):
        """Test that getting non-existent aquarium returns 404."""
        email = f"getaq-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        random_id = str(uuid.uuid4())

        response = await client.get(
            f"/aquariums/{random_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404

    async def test_get_other_user_aquarium_returns_403(self, client: AsyncClient):
        """Test that accessing other user's aquarium returns 403."""
        email1 = f"getaq-owner-{uuid.uuid4()}@example.com"
        email2 = f"getaq-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # User 1 creates aquarium
        create_response = await client.post(
            "/aquariums",
            json={"name": "Private Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # User 2 tries to access
        response = await client.get(
            f"/aquariums/{aquarium_id}",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
class TestUpdateAquarium:
    """Tests for PUT /aquariums/{id} endpoint."""

    async def test_update_aquarium_changes_name(self, client: AsyncClient):
        """Test that update changes aquarium name."""
        email = f"updateaq-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/aquariums",
            json={"name": "Original Name"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # Update
        response = await client.put(
            f"/aquariums/{aquarium_id}",
            json={"name": "Updated Name"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Updated Name"

    async def test_update_nonexistent_aquarium_returns_404(self, client: AsyncClient):
        """Test that updating non-existent aquarium returns 404."""
        email = f"updateaq-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        random_id = str(uuid.uuid4())

        response = await client.put(
            f"/aquariums/{random_id}",
            json={"name": "New Name"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestDeleteAquarium:
    """Tests for DELETE /aquariums/{id} endpoint."""

    async def test_delete_aquarium_returns_204(self, client: AsyncClient):
        """Test that deleting aquarium returns 204."""
        email = f"deleteaq-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/aquariums",
            json={"name": "To Delete"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # Delete
        response = await client.delete(
            f"/aquariums/{aquarium_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 204

    async def test_delete_aquarium_soft_deletes(self, client: AsyncClient):
        """Test that delete is a soft delete (aquarium no longer accessible)."""
        email = f"deleteaq-soft-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Create aquarium
        create_response = await client.post(
            "/aquariums",
            json={"name": "Soft Delete"},
            headers=auth_headers(tokens),
        )
        aquarium_id = create_response.json()["id"]

        # Delete
        await client.delete(
            f"/aquariums/{aquarium_id}",
            headers=auth_headers(tokens),
        )

        # Try to get - should return 404
        response = await client.get(
            f"/aquariums/{aquarium_id}",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 404

    async def test_delete_nonexistent_aquarium_returns_404(self, client: AsyncClient):
        """Test that deleting non-existent aquarium returns 404."""
        email = f"deleteaq-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        random_id = str(uuid.uuid4())

        response = await client.delete(
            f"/aquariums/{random_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404

    async def test_delete_other_user_aquarium_returns_403(self, client: AsyncClient):
        """Test that deleting other user's aquarium returns 403."""
        email1 = f"deleteaq-owner-{uuid.uuid4()}@example.com"
        email2 = f"deleteaq-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # User 1 creates aquarium
        create_response = await client.post(
            "/aquariums",
            json={"name": "Private Tank"},
            headers=auth_headers(tokens1),
        )
        aquarium_id = create_response.json()["id"]

        # User 2 tries to delete
        response = await client.delete(
            f"/aquariums/{aquarium_id}",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403
