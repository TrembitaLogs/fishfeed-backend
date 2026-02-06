"""E2E tests for push notification API endpoints."""

import uuid

import pytest
from httpx import AsyncClient


async def register_and_login(
    client: AsyncClient,
    email: str,
) -> dict:
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
class TestRegisterPushToken:
    """Tests for POST /push/token endpoint."""

    async def test_register_token_returns_201(self, client: AsyncClient):
        """Test that registering push token returns 201."""
        email = f"push-register-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/push/token",
            json={"token": "test_device_token_123", "platform": "ios"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["token"] == "test_device_token_123"
        assert data["platform"] == "ios"
        assert "id" in data
        assert "created_at" in data

    async def test_register_token_android(self, client: AsyncClient):
        """Test registering Android push token."""
        email = f"push-android-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/push/token",
            json={"token": "fcm_token_abc", "platform": "android"},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 201
        assert response.json()["platform"] == "android"

    async def test_register_duplicate_token_updates(self, client: AsyncClient):
        """Test that re-registering same token updates the record."""
        email = f"push-dup-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # First registration
        response1 = await client.post(
            "/api/v1/push/token",
            json={"token": "duplicate_token", "platform": "ios"},
            headers=auth_headers(tokens),
        )
        assert response1.status_code == 201
        token_id1 = response1.json()["id"]

        # Second registration with same token - should update
        response2 = await client.post(
            "/api/v1/push/token",
            json={"token": "duplicate_token", "platform": "android"},
            headers=auth_headers(tokens),
        )
        assert response2.status_code == 201
        token_id2 = response2.json()["id"]
        assert token_id1 == token_id2  # Same record updated
        assert response2.json()["platform"] == "android"

    async def test_register_token_without_auth_returns_401(self, client: AsyncClient):
        """Test that registering token without auth returns 401."""
        response = await client.post(
            "/api/v1/push/token",
            json={"token": "unauthorized_token", "platform": "ios"},
        )
        assert response.status_code == 401

    async def test_register_token_invalid_platform_returns_422(
        self, client: AsyncClient
    ):
        """Test that invalid platform returns 422."""
        email = f"push-invalid-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/push/token",
            json={"token": "some_token", "platform": "windows"},
            headers=auth_headers(tokens),
        )
        assert response.status_code == 422

    async def test_register_token_empty_token_returns_422(self, client: AsyncClient):
        """Test that empty token returns 422."""
        email = f"push-empty-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.post(
            "/api/v1/push/token",
            json={"token": "", "platform": "ios"},
            headers=auth_headers(tokens),
        )
        assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestUnregisterPushToken:
    """Tests for DELETE /push/token endpoint."""

    async def test_unregister_token_returns_204(self, client: AsyncClient):
        """Test that unregistering push token returns 204."""
        email = f"push-unreg-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # First register
        await client.post(
            "/api/v1/push/token",
            json={"token": "token_to_delete", "platform": "ios"},
            headers=auth_headers(tokens),
        )

        # Then unregister
        response = await client.request(
            "DELETE",
            "/api/v1/push/token",
            json={"token": "token_to_delete", "platform": "ios"},
            headers=auth_headers(tokens),
        )
        assert response.status_code == 204

    async def test_unregister_nonexistent_token_returns_404(self, client: AsyncClient):
        """Test that unregistering non-existent token returns 404."""
        email = f"push-unreg-404-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.request(
            "DELETE",
            "/api/v1/push/token",
            json={"token": "nonexistent_token", "platform": "ios"},
            headers=auth_headers(tokens),
        )
        assert response.status_code == 404

    async def test_unregister_token_without_auth_returns_401(self, client: AsyncClient):
        """Test that unregistering token without auth returns 401."""
        response = await client.request(
            "DELETE",
            "/api/v1/push/token",
            json={"token": "some_token", "platform": "ios"},
        )
        assert response.status_code == 401

    async def test_unregister_only_affects_own_token(self, client: AsyncClient):
        """Test that user cannot unregister another user's token."""
        email1 = f"push-user1-{uuid.uuid4()}@example.com"
        email2 = f"push-user2-{uuid.uuid4()}@example.com"

        tokens1 = await register_and_login(client, email1)
        tokens2 = await register_and_login(client, email2)

        # User 1 registers token
        await client.post(
            "/api/v1/push/token",
            json={"token": "user1_token", "platform": "ios"},
            headers=auth_headers(tokens1),
        )

        # User 2 tries to unregister user 1's token - should 404 (not found for user2)
        response = await client.request(
            "DELETE",
            "/api/v1/push/token",
            json={"token": "user1_token", "platform": "ios"},
            headers=auth_headers(tokens2),
        )
        assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
class TestGetNotificationPreferences:
    """Tests for GET /users/me/notifications endpoint."""

    async def test_get_preferences_returns_defaults(self, client: AsyncClient):
        """Test that getting preferences returns defaults for new user."""
        email = f"prefs-default-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.get(
            "/api/v1/users/me/notifications",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["feeding_reminders"] is True
        assert data["overdue_alerts"] is True
        assert data["streak_protection"] is True
        assert data["weekly_summary"] is True
        assert data["family_updates"] is True
        assert data["marketing"] is False
        assert data["updated_at"] is None

    async def test_get_preferences_without_auth_returns_401(self, client: AsyncClient):
        """Test that getting preferences without auth returns 401."""
        response = await client.get("/api/v1/users/me/notifications")
        assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
class TestUpdateNotificationPreferences:
    """Tests for PUT /users/me/notifications endpoint."""

    async def test_update_preferences_returns_200(self, client: AsyncClient):
        """Test that updating preferences returns 200."""
        email = f"prefs-update-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.put(
            "/api/v1/users/me/notifications",
            json={"feeding_reminders": False, "marketing": True},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["feeding_reminders"] is False
        assert data["marketing"] is True
        # Other fields should be defaults
        assert data["overdue_alerts"] is True
        assert data["weekly_summary"] is True

    async def test_update_preferences_persists(self, client: AsyncClient):
        """Test that updated preferences persist."""
        email = f"prefs-persist-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # Update preferences
        await client.put(
            "/api/v1/users/me/notifications",
            json={"weekly_summary": False, "family_updates": False},
            headers=auth_headers(tokens),
        )

        # Get preferences
        response = await client.get(
            "/api/v1/users/me/notifications",
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["weekly_summary"] is False
        assert data["family_updates"] is False
        assert data["updated_at"] is not None

    async def test_update_preferences_partial(self, client: AsyncClient):
        """Test that partial update doesn't affect other fields."""
        email = f"prefs-partial-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        # First update - set some preferences
        await client.put(
            "/api/v1/users/me/notifications",
            json={
                "feeding_reminders": False,
                "overdue_alerts": False,
                "marketing": True,
            },
            headers=auth_headers(tokens),
        )

        # Second update - only change one field
        response = await client.put(
            "/api/v1/users/me/notifications",
            json={"feeding_reminders": True},
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["feeding_reminders"] is True  # Changed
        assert data["overdue_alerts"] is False  # Unchanged
        assert data["marketing"] is True  # Unchanged

    async def test_update_preferences_without_auth_returns_401(
        self, client: AsyncClient
    ):
        """Test that updating preferences without auth returns 401."""
        response = await client.put(
            "/api/v1/users/me/notifications",
            json={"feeding_reminders": False},
        )
        assert response.status_code == 401

    async def test_update_all_preferences(self, client: AsyncClient):
        """Test updating all preference fields."""
        email = f"prefs-all-{uuid.uuid4()}@example.com"
        tokens = await register_and_login(client, email)

        response = await client.put(
            "/api/v1/users/me/notifications",
            json={
                "feeding_reminders": False,
                "overdue_alerts": False,
                "streak_protection": False,
                "weekly_summary": False,
                "family_updates": False,
                "marketing": True,
            },
            headers=auth_headers(tokens),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["feeding_reminders"] is False
        assert data["overdue_alerts"] is False
        assert data["streak_protection"] is False
        assert data["weekly_summary"] is False
        assert data["family_updates"] is False
        assert data["marketing"] is True
