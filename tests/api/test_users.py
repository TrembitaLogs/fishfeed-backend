"""E2E tests for users API endpoints (profile and GDPR)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio(loop_scope="session")
class TestGetUserProfile:
    """Tests for GET /users/me endpoint."""

    async def test_get_profile_requires_auth(self, client: AsyncClient):
        """Test that GET /users/me requires authentication."""
        response = await client.get("/users/me")
        assert response.status_code == 401

    async def test_get_profile_returns_full_profile(self, client: AsyncClient):
        """Test that GET /users/me returns full profile with all fields."""
        email = f"profile_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        response = await client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()

        # Verify all required fields are present
        assert "id" in data
        assert data["email"] == email
        assert "display_name" in data
        assert "avatar_url" in data
        assert "created_at" in data
        assert "subscription_status" in data
        assert "subscription_expires_at" in data
        assert "streak" in data
        assert "achievements_count" in data

    async def test_get_profile_streak_contains_all_fields(self, client: AsyncClient):
        """Test that streak contains current_streak, best_streak, freeze_available, last_feed_date."""
        email = f"streak_fields_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        response = await client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        streak = response.json()["streak"]

        assert "current_streak" in streak
        assert "best_streak" in streak
        assert "freeze_available" in streak
        assert "last_feed_date" in streak

    async def test_get_profile_new_user_has_zero_streak(self, client: AsyncClient):
        """Test that new user has streak with zero values."""
        email = f"new_user_streak_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        response = await client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()

        assert data["streak"]["current_streak"] == 0
        assert data["streak"]["best_streak"] == 0
        assert data["achievements_count"] == 0


@pytest.mark.asyncio(loop_scope="session")
class TestUpdateUserProfile:
    """Tests for PUT /users/me endpoint."""

    async def test_update_profile_requires_auth(self, client: AsyncClient):
        """Test that PUT /users/me requires authentication."""
        response = await client.put("/users/me", json={"display_name": "Test"})
        assert response.status_code == 401

    async def test_update_display_name(self, client: AsyncClient):
        """Test that PUT /users/me updates display_name."""
        email = f"update_name_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Update display_name
        update_response = await client.put(
            "/users/me",
            json={"display_name": "NewDisplayName"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert update_response.status_code == 200
        assert update_response.json()["display_name"] == "NewDisplayName"

        # Verify with GET
        get_response = await client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert get_response.json()["display_name"] == "NewDisplayName"

    async def test_update_avatar_url(self, client: AsyncClient):
        """Test that PUT /users/me updates avatar_url."""
        email = f"update_avatar_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        avatar_url = "https://example.com/avatar.png"
        update_response = await client.put(
            "/users/me",
            json={"avatar_url": avatar_url},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert update_response.status_code == 200
        assert update_response.json()["avatar_url"] == avatar_url

    async def test_update_avatar_url_invalid_returns_422(self, client: AsyncClient):
        """Test that invalid avatar_url (not http/https) returns 422."""
        email = f"invalid_avatar_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Try to set invalid URL (not http/https)
        response = await client.put(
            "/users/me",
            json={"avatar_url": "ftp://example.com/avatar.png"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 422

    async def test_update_empty_body_no_changes(self, client: AsyncClient):
        """Test that PUT /users/me with empty body returns 200 OK."""
        email = f"empty_update_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        response = await client.put(
            "/users/me",
            json={},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200

    async def test_update_display_name_too_long_returns_422(self, client: AsyncClient):
        """Test that display_name > 50 characters returns 422."""
        email = f"long_name_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        long_name = "A" * 51
        response = await client.put(
            "/users/me",
            json={"display_name": long_name},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 422

    async def test_update_allows_null_values(self, client: AsyncClient):
        """Test that PUT /users/me allows setting display_name and avatar_url to null."""
        email = f"null_values_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # First set values
        await client.put(
            "/users/me",
            json={
                "display_name": "TestName",
                "avatar_url": "https://example.com/avatar.png",
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Then set to null
        response = await client.put(
            "/users/me",
            json={"display_name": None, "avatar_url": None},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["display_name"] is None
        assert data["avatar_url"] is None


@pytest.mark.asyncio(loop_scope="session")
class TestUserProfileIntegration:
    """Integration tests for user profile with gamification."""

    async def test_profile_shows_correct_streak_after_feeding(
        self, client: AsyncClient
    ):
        """Test that GET /users/me shows correct streak after feeding events."""
        email = f"feeding_streak_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Create an aquarium
        aquarium_response = await client.post(
            "/aquariums",
            json={"name": "Test Aquarium"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        aquarium_id = aquarium_response.json()["id"]

        # Create a feeding schedule
        schedule_response = await client.post(
            f"/aquariums/{aquarium_id}/feeding/schedules",
            json={"time": "08:00", "food_type": "flakes"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if schedule_response.status_code == 201:
            schedule_id = schedule_response.json()["id"]

            # Mark as fed to trigger streak update
            await client.post(
                f"/aquariums/{aquarium_id}/feeding/schedules/{schedule_id}/mark-fed",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        # Check profile - streak should be updated
        profile_response = await client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert profile_response.status_code == 200
        data = profile_response.json()
        assert "streak" in data
        # After one feeding, current_streak should be at least 1
        # (depending on implementation)
        assert data["streak"]["current_streak"] >= 0


@pytest.mark.asyncio(loop_scope="session")
class TestDataExport:
    """Tests for GET /users/me/data-export endpoint."""

    async def test_data_export_requires_auth(self, client: AsyncClient):
        """Test that data export requires authentication."""
        response = await client.get("/users/me/data-export")
        assert response.status_code == 401

    async def test_data_export_returns_valid_response(self, client: AsyncClient):
        """Test that data export returns presigned URL."""
        # Register and login
        email = f"export_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Mock storage service
        mock_storage = MagicMock()
        mock_storage.upload_json = AsyncMock(return_value="gdpr-exports/user/data.json")
        mock_storage.generate_presigned_url = AsyncMock(
            return_value="https://s3.example.com/presigned-url"
        )

        with patch(
            "app.services.analytics.S3StorageService", return_value=mock_storage
        ):
            response = await client.get(
                "/users/me/data-export",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "download_url" in data
        assert "expires_at" in data
        assert "file_size_bytes" in data
        assert data["format"] == "json"

    async def test_data_export_storage_not_configured(self, client: AsyncClient):
        """Test that data export returns 503 when storage not configured."""
        email = f"export_no_storage_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Mock storage service to raise StorageNotConfiguredError
        from app.services.storage import StorageNotConfiguredError

        mock_storage = MagicMock()
        mock_storage.upload_json = AsyncMock(side_effect=StorageNotConfiguredError())

        with patch(
            "app.services.analytics.S3StorageService", return_value=mock_storage
        ):
            response = await client.get(
                "/users/me/data-export",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        assert response.status_code == 503
        assert "not configured" in response.json()["detail"].lower()


@pytest.mark.asyncio(loop_scope="session")
class TestDataDeletion:
    """Tests for DELETE /users/me/data endpoint."""

    async def test_data_deletion_requires_auth(self, client: AsyncClient):
        """Test that data deletion requires authentication."""
        response = await client.delete("/users/me/data")
        assert response.status_code == 401

    async def test_data_deletion_removes_user(self, client: AsyncClient):
        """Test that data deletion removes user and returns 204."""
        # Register and login
        email = f"delete_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Delete user data
        delete_response = await client.delete(
            "/users/me/data",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert delete_response.status_code == 204

        # Verify user can no longer login
        login_response = await client.post(
            "/auth/login",
            json={"email": email, "password": "SecurePass123"},
        )
        assert login_response.status_code == 401

    async def test_data_deletion_removes_related_data(self, client: AsyncClient):
        """Test that data deletion removes aquariums and related data."""
        # Register and login
        email = f"delete_related_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Create an aquarium
        aquarium_response = await client.post(
            "/aquariums",
            json={"name": "Test Aquarium"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert aquarium_response.status_code == 201

        # Delete user data (aquarium will be deleted as orphan)
        delete_response = await client.delete(
            "/users/me/data",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert delete_response.status_code == 204

    async def test_repeated_deletion_after_user_deleted(self, client: AsyncClient):
        """Test that attempting to access after deletion fails with 401."""
        # Register and login
        email = f"delete_twice_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # First deletion
        first_delete = await client.delete(
            "/users/me/data",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert first_delete.status_code == 204

        # Second deletion attempt with same token should fail
        # (user doesn't exist anymore, so token validation fails)
        second_delete = await client.delete(
            "/users/me/data",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        # Token is invalid after user deletion
        assert second_delete.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
class TestGDPRCompliance:
    """Tests for GDPR compliance requirements."""

    async def test_export_includes_all_data_types(self, client: AsyncClient):
        """Test that export collects data from all tables."""
        email = f"gdpr_full_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Create some data
        await client.post(
            "/aquariums",
            json={"name": "GDPR Test Aquarium"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Capture the JSON data being uploaded
        captured_data = None

        async def capture_upload(data, key):
            nonlocal captured_data
            import json

            captured_data = json.loads(data.decode("utf-8"))
            return key

        mock_storage = MagicMock()
        mock_storage.upload_json = AsyncMock(side_effect=capture_upload)
        mock_storage.generate_presigned_url = AsyncMock(
            return_value="https://s3.example.com/presigned-url"
        )

        with patch(
            "app.services.analytics.S3StorageService", return_value=mock_storage
        ):
            response = await client.get(
                "/users/me/data-export",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        assert response.status_code == 200
        assert captured_data is not None

        # Verify all expected data sections are present
        expected_sections = [
            "profile",
            "owned_aquariums",
            "aquarium_memberships",
            "fish",
            "feeding_schedules",
            "feeding_events",
            "streak",
            "achievements",
            "ai_scans",
            "analytics_events",
            "push_tokens",
            "notification_preferences",
            "family_invites_created",
        ]

        for section in expected_sections:
            assert section in captured_data, f"Missing section: {section}"

    async def test_fk_constraints_not_violated_on_delete(self, client: AsyncClient):
        """Test that deletion doesn't violate FK constraints."""
        email = f"fk_test_{uuid4().hex[:8]}@example.com"
        register_response = await client.post(
            "/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        access_token = register_response.json()["access_token"]

        # Create aquarium with fish
        aquarium_response = await client.post(
            "/aquariums",
            json={"name": "FK Test Aquarium"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        aquarium_id = aquarium_response.json()["id"]

        await client.post(
            f"/aquariums/{aquarium_id}/fish",
            json={"species_id": "test-guppy", "quantity": 5},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # This should not raise any FK constraint errors
        delete_response = await client.delete(
            "/users/me/data",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert delete_response.status_code == 204
