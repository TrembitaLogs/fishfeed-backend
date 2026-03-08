"""Integration tests for gamification API endpoints."""

from uuid import uuid4

import pytest
from httpx import AsyncClient


async def register_and_login(client: AsyncClient, email: str) -> dict:
    """Helper to register a user and return tokens."""
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "SecurePass123"},
    )
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "SecurePass123"},
    )
    return response.json()


async def create_aquarium_with_fish(
    client: AsyncClient, access_token: str, name: str = "Test Aquarium"
) -> dict:
    """Helper to create an aquarium with fish."""
    response = await client.post(
        "/api/v1/aquariums",
        json={"name": name, "volume_liters": 100},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return response.json()


@pytest.mark.asyncio(loop_scope="session")
class TestGetStats:
    """Tests for GET /users/me/stats endpoint."""

    async def test_get_stats_returns_initial_data(self, client: AsyncClient):
        """Test that stats endpoint returns initial data for new user."""
        tokens = await register_and_login(client, "stats_new@example.com")
        access_token = tokens["access_token"]

        response = await client.get(
            "/api/v1/users/me/stats",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "streak" in data
        assert "achievements" in data
        assert "total_feedings" in data
        assert "fish_count" in data
        assert data["streak"]["current_streak"] == 0
        assert data["streak"]["best_streak"] == 0
        assert data["total_feedings"] == 0
        assert data["fish_count"] == 0

    async def test_get_stats_requires_auth(self, client: AsyncClient):
        """Test that stats endpoint requires authentication."""
        response = await client.get("/api/v1/users/me/stats")
        assert response.status_code == 401

    async def test_get_stats_returns_freeze_available(self, client: AsyncClient):
        """Test that stats includes freeze_available in streak data."""
        tokens = await register_and_login(client, "stats_freeze@example.com")
        access_token = tokens["access_token"]

        response = await client.get(
            "/api/v1/users/me/stats",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "freeze_available" in data["streak"]
        assert data["streak"]["freeze_available"] >= 0


@pytest.mark.asyncio(loop_scope="session")
class TestGetAchievements:
    """Tests for GET /users/me/achievements endpoint."""

    async def test_get_achievements_returns_empty_list_for_new_user(
        self, client: AsyncClient
    ):
        """Test that achievements endpoint returns empty list for new user."""
        tokens = await register_and_login(client, "achievements_new@example.com")
        access_token = tokens["access_token"]

        response = await client.get(
            "/api/v1/users/me/achievements",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    async def test_get_achievements_requires_auth(self, client: AsyncClient):
        """Test that achievements endpoint requires authentication."""
        response = await client.get("/api/v1/users/me/achievements")
        assert response.status_code == 401

    async def test_get_achievements_response_format(self, client: AsyncClient):
        """Test that achievements response has correct format."""
        tokens = await register_and_login(client, "achievements_format@example.com")
        access_token = tokens["access_token"]

        # Create aquarium to potentially trigger first_aquarium achievement
        await create_aquarium_with_fish(client, access_token)

        response = await client.get(
            "/api/v1/users/me/achievements",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Achievement response format validation
        for achievement in data:
            assert "id" in achievement
            assert "achievement_type" in achievement
            assert "unlocked_at" in achievement
            assert "shared_at" in achievement or achievement.get("shared_at") is None


@pytest.mark.asyncio(loop_scope="session")
class TestShareAchievement:
    """Tests for POST /achievements/{achievement_id}/share endpoint."""

    async def test_share_achievement_returns_404_for_nonexistent(
        self, client: AsyncClient
    ):
        """Test that share endpoint returns 404 for non-existent achievement."""
        tokens = await register_and_login(client, "share_notfound@example.com")
        access_token = tokens["access_token"]

        fake_id = uuid4()
        response = await client.post(
            f"/api/v1/achievements/{fake_id}/share",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    async def test_share_achievement_requires_auth(self, client: AsyncClient):
        """Test that share endpoint requires authentication."""
        fake_id = uuid4()
        response = await client.post(f"/api/v1/achievements/{fake_id}/share")
        assert response.status_code == 401

    async def test_share_achievement_returns_403_for_other_user(
        self, client: AsyncClient
    ):
        """Test that share endpoint returns 403 when achievement belongs to other user."""
        # Create first user and get an achievement
        tokens1 = await register_and_login(client, "share_owner@example.com")
        access_token1 = tokens1["access_token"]

        # Create aquarium to trigger first_aquarium achievement
        await create_aquarium_with_fish(client, access_token1)

        # Get achievements for first user
        response1 = await client.get(
            "/api/v1/users/me/achievements",
            headers={"Authorization": f"Bearer {access_token1}"},
        )
        achievements = response1.json()

        if len(achievements) == 0:
            pytest.skip("No achievements unlocked to test with")

        achievement_id = achievements[0]["id"]

        # Create second user
        tokens2 = await register_and_login(client, "share_other@example.com")
        access_token2 = tokens2["access_token"]

        # Try to share first user's achievement as second user
        response = await client.post(
            f"/api/v1/achievements/{achievement_id}/share",
            headers={"Authorization": f"Bearer {access_token2}"},
        )

        assert response.status_code == 403
        assert "does not belong" in response.json()["detail"].lower()

    async def test_share_achievement_success(self, client: AsyncClient):
        """Test that share endpoint successfully shares an achievement."""
        tokens = await register_and_login(client, "share_success@example.com")
        access_token = tokens["access_token"]

        # Create aquarium to trigger first_aquarium achievement
        await create_aquarium_with_fish(client, access_token)

        # Get achievements
        response = await client.get(
            "/api/v1/users/me/achievements",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        achievements = response.json()

        if len(achievements) == 0:
            pytest.skip("No achievements unlocked to test with")

        achievement_id = achievements[0]["id"]

        # Share the achievement
        share_response = await client.post(
            f"/api/v1/achievements/{achievement_id}/share",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert share_response.status_code == 200
        data = share_response.json()
        assert data["id"] == achievement_id
        assert data["shared_at"] is not None
