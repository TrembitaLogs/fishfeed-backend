"""Tests for authentication Pydantic schemas."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.auth import (
    LoginRequest,
    OAuthRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)


class TestRegisterRequest:
    """Tests for RegisterRequest schema."""

    def test_valid_registration(self):
        """Test valid registration request."""
        request = RegisterRequest(
            email="test@example.com",
            password="Password123",
        )
        assert request.email == "test@example.com"
        assert request.password == "Password123"

    def test_invalid_email_format(self):
        """Test that invalid email format is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RegisterRequest(email="invalid-email", password="Password123")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("email",)

    def test_password_too_short(self):
        """Test that password shorter than 8 characters is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RegisterRequest(email="test@example.com", password="Pass1")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("password",)
        assert "string_too_short" in errors[0]["type"]

    def test_password_missing_uppercase(self):
        """Test that password without uppercase letter is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RegisterRequest(email="test@example.com", password="password123")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("password",)
        assert "uppercase" in str(errors[0]["msg"]).lower()

    def test_password_missing_lowercase(self):
        """Test that password without lowercase letter is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RegisterRequest(email="test@example.com", password="PASSWORD123")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("password",)
        assert "lowercase" in str(errors[0]["msg"]).lower()

    def test_password_missing_digit(self):
        """Test that password without digit is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RegisterRequest(email="test@example.com", password="PasswordAbc")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("password",)
        assert "digit" in str(errors[0]["msg"]).lower()

    def test_password_exactly_8_chars(self):
        """Test that password with exactly 8 characters is accepted."""
        request = RegisterRequest(email="test@example.com", password="Pass123a")
        assert request.password == "Pass123a"


class TestLoginRequest:
    """Tests for LoginRequest schema."""

    def test_valid_login(self):
        """Test valid login request."""
        request = LoginRequest(
            email="test@example.com",
            password="anypassword",
        )
        assert request.email == "test@example.com"
        assert request.password == "anypassword"

    def test_invalid_email(self):
        """Test that invalid email is rejected."""
        with pytest.raises(ValidationError):
            LoginRequest(email="not-an-email", password="password")


class TestOAuthRequest:
    """Tests for OAuthRequest schema."""

    def test_google_provider(self):
        """Test OAuth request with Google provider."""
        request = OAuthRequest(provider="google", token="some-token")
        assert request.provider == "google"
        assert request.token == "some-token"

    def test_apple_provider(self):
        """Test OAuth request with Apple provider."""
        request = OAuthRequest(provider="apple", token="some-token")
        assert request.provider == "apple"

    def test_invalid_provider(self):
        """Test that invalid provider is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            OAuthRequest(provider="facebook", token="some-token")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("provider",)

    def test_empty_token(self):
        """Test that empty token is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            OAuthRequest(provider="google", token="")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("token",)


class TestUserResponse:
    """Tests for UserResponse schema."""

    def test_serialization(self):
        """Test UserResponse serializes correctly."""
        user_id = uuid4()
        created = datetime.now(UTC)

        response = UserResponse(
            id=user_id,
            email="test@example.com",
            created_at=created,
        )

        assert response.id == user_id
        assert response.email == "test@example.com"
        assert response.created_at == created

    def test_from_attributes(self):
        """Test UserResponse can be created from ORM model."""

        class MockUser:
            def __init__(self):
                self.id = uuid4()
                self.email = "orm@example.com"
                self.created_at = datetime.now(UTC)

        mock_user = MockUser()
        response = UserResponse.model_validate(mock_user)

        assert response.id == mock_user.id
        assert response.email == mock_user.email
        assert response.created_at == mock_user.created_at


class TestTokenResponse:
    """Tests for TokenResponse schema."""

    def test_serialization(self):
        """Test TokenResponse serializes correctly."""
        user_id = uuid4()
        created = datetime.now(UTC)

        response = TokenResponse(
            access_token="access.token.here",
            refresh_token="refresh.token.here",
            user=UserResponse(
                id=user_id,
                email="test@example.com",
                created_at=created,
            ),
        )

        assert response.access_token == "access.token.here"
        assert response.refresh_token == "refresh.token.here"
        assert response.token_type == "bearer"
        assert response.user.id == user_id

    def test_default_token_type(self):
        """Test that token_type defaults to 'bearer'."""
        response = TokenResponse(
            access_token="access",
            refresh_token="refresh",
            user=UserResponse(
                id=uuid4(),
                email="test@example.com",
                created_at=datetime.now(UTC),
            ),
        )
        assert response.token_type == "bearer"

    def test_to_dict(self):
        """Test TokenResponse can be serialized to dict."""
        user_id = uuid4()
        created = datetime.now(UTC)

        response = TokenResponse(
            access_token="access",
            refresh_token="refresh",
            user=UserResponse(
                id=user_id,
                email="test@example.com",
                created_at=created,
            ),
        )

        data = response.model_dump()
        assert data["access_token"] == "access"
        assert data["refresh_token"] == "refresh"
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == "test@example.com"


class TestRefreshRequest:
    """Tests for RefreshRequest schema."""

    def test_valid_refresh(self):
        """Test valid refresh request."""
        request = RefreshRequest(refresh_token="some.refresh.token")
        assert request.refresh_token == "some.refresh.token"

    def test_empty_token(self):
        """Test that empty refresh token is rejected."""
        with pytest.raises(ValidationError):
            RefreshRequest(refresh_token="")


class TestPasswordResetRequest:
    """Tests for PasswordResetRequest schema."""

    def test_valid_reset(self):
        """Test valid password reset request."""
        request = PasswordResetRequest(email="test@example.com")
        assert request.email == "test@example.com"

    def test_invalid_email(self):
        """Test that invalid email is rejected."""
        with pytest.raises(ValidationError):
            PasswordResetRequest(email="not-an-email")


class TestPasswordChangeRequest:
    """Tests for PasswordChangeRequest schema."""

    def test_valid_change(self):
        """Test valid password change request."""
        request = PasswordChangeRequest(
            old_password="OldPass123",
            new_password="NewPass456",
        )
        assert request.old_password == "OldPass123"
        assert request.new_password == "NewPass456"

    def test_new_password_complexity(self):
        """Test that new password must meet complexity requirements."""
        with pytest.raises(ValidationError) as exc_info:
            PasswordChangeRequest(
                old_password="anything",
                new_password="simple",
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("new_password",) for e in errors)

    def test_new_password_too_short(self):
        """Test that short new password is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            PasswordChangeRequest(
                old_password="anything",
                new_password="Pass1",
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("new_password",)
