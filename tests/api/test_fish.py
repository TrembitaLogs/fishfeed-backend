"""E2E tests for fish API endpoints."""

import uuid

import pytest
from httpx import AsyncClient

# Pre-seeded test species IDs from conftest.py
TEST_SPECIES_GUPPY = "test-guppy"
TEST_SPECIES_BETTA = "test-betta"


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


@pytest.mark.asyncio(loop_scope="session")
class TestListFish:
    """Tests for GET /aquariums/{id}/fish endpoint."""

    async def test_list_fish_without_auth_returns_401(self, client: AsyncClient):
        """Test that listing fish without auth returns 401."""
        random_id = str(uuid.uuid4())
        response = await client.get(f"/api/v1/aquariums/{random_id}/fish")
        assert response.status_code == 401

    async def test_list_fish_returns_empty_list(self, client: AsyncClient):
        """Test that new aquarium has empty fish list."""
        email = f"listfish-empty-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        assert response.json() == []

    async def test_list_fish_returns_aquarium_fish(self, client: AsyncClient):
        """Test that list returns aquarium's fish."""
        email = f"listfish-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Add fish using pre-seeded species
        await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_GUPPY, "quantity": 5},
            headers=auth_headers(tokens),
        )

        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["species_id"] == TEST_SPECIES_GUPPY
        assert data[0]["quantity"] == 5

    async def test_list_fish_other_user_aquarium_returns_403(self, client: AsyncClient):
        """Test that listing fish in other user's aquarium returns 403."""
        email1 = f"listfish-owner-{uuid.uuid4()}@example.com"
        email2 = f"listfish-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        aquarium_id = await create_aquarium(client, tokens1)

        response = await client.get(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403

    async def test_list_fish_nonexistent_aquarium_returns_404(self, client: AsyncClient):
        """Test that listing fish in non-existent aquarium returns 404."""
        email = f"listfish-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        random_id = str(uuid.uuid4())

        response = await client.get(
            f"/api/v1/aquariums/{random_id}/fish",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestAddFish:
    """Tests for POST /aquariums/{id}/fish endpoint."""

    async def test_add_fish_returns_201(self, client: AsyncClient):
        """Test that adding fish returns 201."""
        email = f"addfish-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={
                "species_id": TEST_SPECIES_GUPPY,
                "quantity": 10,
                "custom_name": "My Guppies",
            },
            headers=auth_headers(tokens),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["species_id"] == TEST_SPECIES_GUPPY
        assert data["quantity"] == 10
        assert data["custom_name"] == "My Guppies"
        assert "id" in data

    async def test_add_fish_without_auth_returns_401(self, client: AsyncClient):
        """Test that adding fish without auth returns 401."""
        random_id = str(uuid.uuid4())
        response = await client.post(
            f"/api/v1/aquariums/{random_id}/fish",
            json={"species_id": "guppy", "quantity": 1},
        )
        assert response.status_code == 401

    async def test_add_fish_nonexistent_species_returns_404(self, client: AsyncClient):
        """Test that adding fish with non-existent species returns 404."""
        email = f"addfish-nospecies-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": "nonexistent_species_xyz"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404

    async def test_add_fish_other_user_aquarium_returns_403(self, client: AsyncClient):
        """Test that adding fish to other user's aquarium returns 403."""
        email1 = f"addfish-owner-{uuid.uuid4()}@example.com"
        email2 = f"addfish-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        aquarium_id = await create_aquarium(client, tokens1)

        response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": "guppy"},
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
class TestGetFish:
    """Tests for GET /fish/{id} endpoint."""

    async def test_get_fish_returns_details(self, client: AsyncClient):
        """Test that get fish returns details."""
        email = f"getfish-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Add fish using pre-seeded species
        add_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_BETTA, "quantity": 1, "custom_name": "Bluey"},
            headers=auth_headers(tokens),
        )
        fish_id = add_response.json()["id"]

        response = await client.get(
            f"/api/v1/fish/{fish_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == fish_id
        assert data["custom_name"] == "Bluey"

    async def test_get_fish_nonexistent_returns_404(self, client: AsyncClient):
        """Test that getting non-existent fish returns 404."""
        email = f"getfish-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        random_id = str(uuid.uuid4())

        response = await client.get(
            f"/api/v1/fish/{random_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404

    async def test_get_fish_other_user_returns_403(self, client: AsyncClient):
        """Test that getting other user's fish returns 403."""
        email1 = f"getfish-owner-{uuid.uuid4()}@example.com"
        email2 = f"getfish-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        aquarium_id = await create_aquarium(client, tokens1)

        # User 1 adds fish
        add_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_GUPPY},
            headers=auth_headers(tokens1),
        )
        fish_id = add_response.json()["id"]

        # User 2 tries to get
        response = await client.get(
            f"/api/v1/fish/{fish_id}",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
class TestUpdateFish:
    """Tests for PUT /fish/{id} endpoint."""

    async def test_update_fish_changes_data(self, client: AsyncClient):
        """Test that update changes fish data."""
        email = f"updatefish-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Add fish
        add_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_GUPPY, "quantity": 1},
            headers=auth_headers(tokens),
        )
        fish_id = add_response.json()["id"]

        # Update
        response = await client.put(
            f"/api/v1/fish/{fish_id}",
            json={"quantity": 5, "custom_name": "New Name"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["quantity"] == 5
        assert data["custom_name"] == "New Name"

    async def test_update_fish_nonexistent_returns_404(self, client: AsyncClient):
        """Test that updating non-existent fish returns 404."""
        email = f"updatefish-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        random_id = str(uuid.uuid4())

        response = await client.put(
            f"/api/v1/fish/{random_id}",
            json={"quantity": 10},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestDeleteFish:
    """Tests for DELETE /fish/{id} endpoint."""

    async def test_delete_fish_returns_204(self, client: AsyncClient):
        """Test that deleting fish returns 204."""
        email = f"deletefish-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Add fish
        add_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_GUPPY},
            headers=auth_headers(tokens),
        )
        fish_id = add_response.json()["id"]

        # Delete
        response = await client.delete(
            f"/api/v1/fish/{fish_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 204

    async def test_delete_fish_soft_deletes(self, client: AsyncClient):
        """Test that delete is a soft delete (fish no longer accessible)."""
        email = f"deletefish-soft-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        aquarium_id = await create_aquarium(client, tokens)

        # Add fish
        add_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_BETTA},
            headers=auth_headers(tokens),
        )
        fish_id = add_response.json()["id"]

        # Delete
        await client.delete(
            f"/api/v1/fish/{fish_id}",
            headers=auth_headers(tokens),
        )

        # Try to get - should return 404
        response = await client.get(
            f"/api/v1/fish/{fish_id}",
            headers=auth_headers(tokens),
        )
        assert response.status_code == 404

    async def test_delete_fish_nonexistent_returns_404(self, client: AsyncClient):
        """Test that deleting non-existent fish returns 404."""
        email = f"deletefish-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)
        random_id = str(uuid.uuid4())

        response = await client.delete(
            f"/api/v1/fish/{random_id}",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 404

    async def test_delete_other_user_fish_returns_403(self, client: AsyncClient):
        """Test that deleting other user's fish returns 403."""
        email1 = f"deletefish-owner-{uuid.uuid4()}@example.com"
        email2 = f"deletefish-other-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        aquarium_id = await create_aquarium(client, tokens1)

        # User 1 adds fish
        add_response = await client.post(
            f"/api/v1/aquariums/{aquarium_id}/fish",
            json={"species_id": TEST_SPECIES_GUPPY},
            headers=auth_headers(tokens1),
        )
        fish_id = add_response.json()["id"]

        # User 2 tries to delete
        response = await client.delete(
            f"/api/v1/fish/{fish_id}",
            headers=auth_headers(tokens2),
        )

        assert response.status_code == 403
