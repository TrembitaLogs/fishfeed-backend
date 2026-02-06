"""E2E tests for authentication API endpoints."""

from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.services.auth import InvalidOAuthTokenError, OAuthNotConfiguredError


@pytest.mark.asyncio(loop_scope="session")
class TestRegister:
    """Tests for POST /auth/register endpoint."""

    async def test_register_creates_user(self, client: AsyncClient):
        """Test that registration creates a new user and returns tokens."""
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "newuser@example.com",
                "password": "SecurePass123",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == "newuser@example.com"

    async def test_register_duplicate_email_fails(self, client: AsyncClient):
        """Test that registration with existing email fails."""
        email = "duplicate@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "AnotherPass456"},
        )
        assert response.status_code == 409
        assert "already registered" in response.json()["detail"].lower()

    async def test_register_weak_password_fails(self, client: AsyncClient):
        """Test that registration with weak password fails validation."""
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "weakpass@example.com", "password": "weak"},
        )
        assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
class TestLogin:
    """Tests for POST /auth/login endpoint."""

    async def test_login_returns_tokens(self, client: AsyncClient):
        """Test that login returns valid tokens."""
        email = "logintest@example.com"
        password = "SecurePass123"
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password},
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == email

    async def test_login_invalid_credentials_fails(self, client: AsyncClient):
        """Test that login with invalid credentials returns 401."""
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent@example.com", "password": "WrongPass123"},
        )
        assert response.status_code == 401

    async def test_login_wrong_password_fails(self, client: AsyncClient):
        """Test that login with wrong password returns 401."""
        email = "wrongpass@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "CorrectPass123"},
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "WrongPass456"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
class TestRefresh:
    """Tests for POST /auth/refresh endpoint."""

    async def test_refresh_with_valid_token_works(self, client: AsyncClient):
        """Test that refresh with valid token returns new tokens."""
        email = "refreshtest@example.com"
        password = "SecurePass123"
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password},
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        refresh_token = login_response.json()["refresh_token"]

        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["refresh_token"] != refresh_token

    async def test_refresh_with_invalid_token_fails(self, client: AsyncClient):
        """Test that refresh with invalid token returns 401."""
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid.token.here"},
        )
        assert response.status_code == 401

    async def test_refresh_token_rotation_invalidates_old_token(
        self, client: AsyncClient
    ):
        """Test that old refresh token is invalidated after rotation."""
        email = "rotation@example.com"
        password = "SecurePass123"
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password},
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        old_refresh_token = login_response.json()["refresh_token"]

        await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old_refresh_token},
        )

        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old_refresh_token},
        )
        assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
class TestLogout:
    """Tests for POST /auth/logout endpoint."""

    async def test_logout_invalidates_token(self, client: AsyncClient):
        """Test that logout invalidates the refresh token."""
        email = "logouttest@example.com"
        password = "SecurePass123"
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password},
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        tokens = login_response.json()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        logout_response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refresh_token},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert logout_response.status_code == 204

        refresh_response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert refresh_response.status_code == 401

    async def test_logout_requires_auth(self, client: AsyncClient):
        """Test that logout requires authentication."""
        response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "some.token.here"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
class TestTokenValidation:
    """Tests for token validation behavior."""

    async def test_expired_access_token_returns_401(self, client: AsyncClient):
        """Test that expired/invalid access token returns 401."""
        response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "some.token"},
            headers={"Authorization": "Bearer expired.invalid.token"},
        )
        assert response.status_code == 401

    async def test_malformed_authorization_header_returns_401(
        self, client: AsyncClient
    ):
        """Test that malformed authorization header returns 401."""
        response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "some.token"},
            headers={"Authorization": "NotBearer token"},
        )
        assert response.status_code == 401

    async def test_missing_authorization_header_returns_401(self, client: AsyncClient):
        """Test that missing authorization header returns 401."""
        response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "some.token"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
class TestPasswordChange:
    """Tests for POST /auth/password/change endpoint."""

    async def test_password_change_success(self, client: AsyncClient):
        """Test that password change works with correct old password."""
        email = "passchange@example.com"
        old_password = "OldSecurePass123"
        new_password = "NewSecurePass456"

        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": old_password},
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": old_password},
        )
        access_token = login_response.json()["access_token"]

        change_response = await client.post(
            "/api/v1/auth/password/change",
            json={"old_password": old_password, "new_password": new_password},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert change_response.status_code == 200

        new_login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": new_password},
        )
        assert new_login_response.status_code == 200

    async def test_password_change_wrong_old_password_fails(self, client: AsyncClient):
        """Test that password change fails with wrong old password."""
        email = "wrongoldpass@example.com"
        password = "SecurePass123"

        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password},
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        access_token = login_response.json()["access_token"]

        response = await client.post(
            "/api/v1/auth/password/change",
            json={"old_password": "WrongOldPass123", "new_password": "NewPass456"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert response.status_code == 400


@pytest.mark.asyncio(loop_scope="session")
class TestPasswordReset:
    """Tests for POST /auth/password/reset endpoint."""

    async def test_password_reset_always_succeeds(self, client: AsyncClient):
        """Test that password reset always returns 202 to prevent enumeration."""
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={"email": "nonexistent@example.com"},
        )
        assert response.status_code == 202

        email = "existinguser@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "SecurePass123"},
        )
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={"email": email},
        )
        assert response.status_code == 202


@pytest.mark.asyncio(loop_scope="session")
class TestAccountDeletion:
    """Tests for DELETE /auth/account endpoint."""

    async def test_account_deletion_soft_deletes(self, client: AsyncClient):
        """Test that account deletion performs soft delete."""
        email = "deleteaccount@example.com"
        password = "SecurePass123"

        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password},
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        access_token = login_response.json()["access_token"]

        delete_response = await client.delete(
            "/api/v1/auth/account",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert delete_response.status_code == 204

        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_response.status_code == 401

    async def test_account_deletion_requires_auth(self, client: AsyncClient):
        """Test that account deletion requires authentication."""
        response = await client.delete("/api/v1/auth/account")
        assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
class TestOAuth:
    """Tests for POST /auth/oauth endpoint."""

    async def test_oauth_google_creates_user(self, client: AsyncClient):
        """Test that Google OAuth creates a new user and returns tokens."""
        mock_token_info = {
            "email": "oauth_google_api@example.com",
            "sub": "google_api_12345",
        }

        with patch(
            "app.services.auth._verify_google_token",
            return_value=mock_token_info,
        ):
            response = await client.post(
                "/api/v1/auth/oauth",
                json={"provider": "google", "token": "fake_google_token"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == "oauth_google_api@example.com"

    async def test_oauth_apple_creates_user(self, client: AsyncClient):
        """Test that Apple OAuth creates a new user and returns tokens."""
        mock_token_info = {
            "email": "oauth_apple_api@example.com",
            "sub": "apple_api_67890",
        }

        with patch(
            "app.services.auth._verify_apple_token",
            return_value=mock_token_info,
        ):
            response = await client.post(
                "/api/v1/auth/oauth",
                json={"provider": "apple", "token": "fake_apple_token"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["user"]["email"] == "oauth_apple_api@example.com"

    async def test_oauth_existing_user_returns_tokens(self, client: AsyncClient):
        """Test that OAuth login for existing user returns tokens."""
        mock_token_info = {
            "email": "oauth_existing_api@example.com",
            "sub": "google_existing_api",
        }

        with patch(
            "app.services.auth._verify_google_token",
            return_value=mock_token_info,
        ):
            # First OAuth login creates user
            first_response = await client.post(
                "/api/v1/auth/oauth",
                json={"provider": "google", "token": "fake_token_1"},
            )
            user_id = first_response.json()["user"]["id"]

            # Second OAuth login returns same user
            second_response = await client.post(
                "/api/v1/auth/oauth",
                json={"provider": "google", "token": "fake_token_2"},
            )

        assert second_response.status_code == 200
        assert second_response.json()["user"]["id"] == user_id

    async def test_oauth_invalid_token_returns_401(self, client: AsyncClient):
        """Test that invalid OAuth token returns 401."""
        with patch(
            "app.services.auth._verify_google_token",
            side_effect=InvalidOAuthTokenError("Token validation failed"),
        ):
            response = await client.post(
                "/api/v1/auth/oauth",
                json={"provider": "google", "token": "invalid_token"},
            )

        assert response.status_code == 401
        assert "Token validation failed" in response.json()["detail"]

    async def test_oauth_not_configured_returns_500(self, client: AsyncClient):
        """Test that unconfigured OAuth provider returns 500."""
        with patch(
            "app.services.auth._verify_google_token",
            side_effect=OAuthNotConfiguredError("google"),
        ):
            response = await client.post(
                "/api/v1/auth/oauth",
                json={"provider": "google", "token": "some_token"},
            )

        assert response.status_code == 500
        assert "not configured" in response.json()["detail"]

    async def test_oauth_invalid_provider_returns_422(self, client: AsyncClient):
        """Test that invalid OAuth provider returns 422."""
        response = await client.post(
            "/api/v1/auth/oauth",
            json={"provider": "invalid_provider", "token": "some_token"},
        )
        assert response.status_code == 422
