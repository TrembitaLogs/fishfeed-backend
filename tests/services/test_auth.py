"""Integration tests for authentication service."""

from unittest.mock import patch

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.auth import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidOAuthTokenError,
    InvalidRefreshTokenError,
    OAuthNotConfiguredError,
    login_user,
    logout,
    oauth_login,
    refresh_tokens,
    register_user,
)
from app.utils.jwt import decode_token


async def cleanup_users(session: AsyncSession) -> None:
    """Helper to cleanup users and related data."""
    # Use TRUNCATE CASCADE to handle foreign key constraints
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.commit()


async def cleanup_redis(redis: Redis) -> None:
    """Helper to cleanup Redis."""
    await redis.flushdb()


@pytest.mark.asyncio(loop_scope="session")
async def test_register_user_creates_user_in_db(
    async_session: AsyncSession,
):
    """Test that register_user creates a new user in the database."""
    await cleanup_users(async_session)
    try:
        email = "test_register@example.com"
        password = "Password123"

        user = await register_user(async_session, email, password)

        assert user is not None
        assert user.id is not None
        assert user.email == email
        assert user.password_hash is not None
        assert user.password_hash != password
    finally:
        await cleanup_users(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_register_user_fails_if_email_exists(
    async_session: AsyncSession,
):
    """Test that register_user raises error if email already exists."""
    await cleanup_users(async_session)
    try:
        email = "test_duplicate@example.com"
        password = "Password123"

        await register_user(async_session, email, password)

        with pytest.raises(EmailAlreadyExistsError):
            await register_user(async_session, email, "AnotherPass123")
    finally:
        await cleanup_users(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_login_user_returns_tokens_with_correct_credentials(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that login_user returns tokens when credentials are valid."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        email = "test_login@example.com"
        password = "Password123"

        await register_user(async_session, email, password)

        token_response = await login_user(async_session, redis_client, email, password)

        assert token_response is not None
        assert token_response.access_token is not None
        assert token_response.refresh_token is not None
        assert token_response.token_type == "bearer"
        assert token_response.user.email == email
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_login_user_stores_refresh_token_in_redis(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that login_user stores the refresh token in Redis."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        email = "test_login_redis@example.com"
        password = "Password123"

        await register_user(async_session, email, password)
        token_response = await login_user(async_session, redis_client, email, password)

        payload = decode_token(token_response.refresh_token)
        assert payload is not None

        jti = payload["jti"]
        stored_value = await redis_client.get(f"refresh:{jti}")
        assert stored_value is not None
        assert stored_value == str(token_response.user.id)
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_login_user_fails_with_wrong_password(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that login_user raises error with wrong password."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        email = "test_wrong_pass@example.com"
        password = "Password123"

        await register_user(async_session, email, password)

        with pytest.raises(InvalidCredentialsError):
            await login_user(async_session, redis_client, email, "WrongPassword123")
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_login_user_fails_with_nonexistent_email(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that login_user raises error with nonexistent email."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        with pytest.raises(InvalidCredentialsError):
            await login_user(async_session, redis_client, "nouser@example.com", "Password123")
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_refresh_tokens_returns_new_tokens(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that refresh_tokens returns new access and refresh tokens."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        email = "test_refresh@example.com"
        password = "Password123"

        await register_user(async_session, email, password)
        initial_tokens = await login_user(async_session, redis_client, email, password)

        new_tokens = await refresh_tokens(
            async_session,
            redis_client,
            initial_tokens.refresh_token,
        )

        # Refresh tokens must be different (have unique jti)
        assert new_tokens.refresh_token != initial_tokens.refresh_token
        # Access tokens are validated
        assert new_tokens.access_token is not None
        assert len(new_tokens.access_token) > 0
        assert new_tokens.user.email == email
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_refresh_tokens_invalidates_old_token(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that refresh_tokens invalidates the old refresh token."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        email = "test_refresh_invalidate@example.com"
        password = "Password123"

        await register_user(async_session, email, password)
        initial_tokens = await login_user(async_session, redis_client, email, password)
        old_refresh_token = initial_tokens.refresh_token

        await refresh_tokens(async_session, redis_client, old_refresh_token)

        with pytest.raises(InvalidRefreshTokenError):
            await refresh_tokens(async_session, redis_client, old_refresh_token)
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_refresh_tokens_fails_with_invalid_token(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that refresh_tokens raises error with invalid token."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        with pytest.raises(InvalidRefreshTokenError):
            await refresh_tokens(async_session, redis_client, "invalid.token.here")
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_refresh_tokens_fails_with_expired_token_not_in_redis(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that refresh_tokens raises error when token is not in Redis."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        email = "test_refresh_not_in_redis@example.com"
        password = "Password123"

        await register_user(async_session, email, password)
        tokens = await login_user(async_session, redis_client, email, password)

        # Remove token from Redis
        payload = decode_token(tokens.refresh_token)
        await redis_client.delete(f"refresh:{payload['jti']}")

        with pytest.raises(InvalidRefreshTokenError):
            await refresh_tokens(async_session, redis_client, tokens.refresh_token)
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_logout_removes_token_from_redis(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that logout removes the refresh token from Redis."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        email = "test_logout@example.com"
        password = "Password123"

        await register_user(async_session, email, password)
        tokens = await login_user(async_session, redis_client, email, password)

        payload = decode_token(tokens.refresh_token)
        jti = payload["jti"]

        assert await redis_client.get(f"refresh:{jti}") is not None

        await logout(redis_client, tokens.refresh_token)

        assert await redis_client.get(f"refresh:{jti}") is None
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_logout_with_invalid_token_does_not_raise(
    redis_client: Redis,
):
    """Test that logout with invalid token does not raise error."""
    await cleanup_redis(redis_client)
    try:
        await logout(redis_client, "invalid.token.here")
    finally:
        await cleanup_redis(redis_client)


# OAuth Tests


@pytest.mark.asyncio(loop_scope="session")
async def test_oauth_login_google_creates_new_user(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that Google OAuth creates a new user when not exists."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        mock_token_info = {
            "email": "google_user@example.com",
            "sub": "google_12345",
        }

        with patch(
            "app.services.auth._verify_google_token",
            return_value=mock_token_info,
        ):
            token_response = await oauth_login(
                async_session, redis_client, "google", "fake_google_token"
            )

        assert token_response is not None
        assert token_response.access_token is not None
        assert token_response.refresh_token is not None
        assert token_response.user.email == "google_user@example.com"

        # Verify user was created in database
        stmt = select(User).where(User.email == "google_user@example.com")
        result = await async_session.execute(stmt)
        user = result.scalar_one()
        assert user.oauth_provider == "google"
        assert user.oauth_id == "google_12345"
        assert user.email_verified is True
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_oauth_login_apple_creates_new_user(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that Apple OAuth creates a new user when not exists."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        mock_token_info = {
            "email": "apple_user@example.com",
            "sub": "apple_67890",
        }

        with patch(
            "app.services.auth._verify_apple_token",
            return_value=mock_token_info,
        ):
            token_response = await oauth_login(
                async_session, redis_client, "apple", "fake_apple_token"
            )

        assert token_response is not None
        assert token_response.access_token is not None
        assert token_response.refresh_token is not None
        assert token_response.user.email == "apple_user@example.com"

        # Verify user was created in database
        stmt = select(User).where(User.email == "apple_user@example.com")
        result = await async_session.execute(stmt)
        user = result.scalar_one()
        assert user.oauth_provider == "apple"
        assert user.oauth_id == "apple_67890"
        assert user.email_verified is True
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_oauth_login_returns_existing_user(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that OAuth login returns existing user by oauth_id."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        # Create user first via OAuth
        mock_token_info = {
            "email": "existing_oauth@example.com",
            "sub": "oauth_existing_123",
        }

        with patch(
            "app.services.auth._verify_google_token",
            return_value=mock_token_info,
        ):
            first_response = await oauth_login(
                async_session, redis_client, "google", "fake_token_1"
            )
            user_id = first_response.user.id

            # Login again with same OAuth
            second_response = await oauth_login(
                async_session, redis_client, "google", "fake_token_2"
            )

        assert second_response.user.id == user_id
        assert second_response.user.email == "existing_oauth@example.com"
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_oauth_login_links_to_existing_email_account(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that OAuth links to existing account with same email."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        # Create user with email/password first
        email = "linktest@example.com"
        password = "Password123"
        await register_user(async_session, email, password)

        # Login via OAuth with same email
        mock_token_info = {
            "email": email,
            "sub": "google_link_123",
        }

        with patch(
            "app.services.auth._verify_google_token",
            return_value=mock_token_info,
        ):
            await oauth_login(
                async_session, redis_client, "google", "fake_token"
            )

        # Verify OAuth was linked
        stmt = select(User).where(User.email == email)
        result = await async_session.execute(stmt)
        user = result.scalar_one()
        assert user.oauth_provider == "google"
        assert user.oauth_id == "google_link_123"
        assert user.password_hash is not None  # Password still exists
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_oauth_login_invalid_token_fails(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that OAuth login fails with invalid token."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        with patch(
            "app.services.auth._verify_google_token",
            side_effect=InvalidOAuthTokenError("Token validation failed"),
        ):
            with pytest.raises(InvalidOAuthTokenError):
                await oauth_login(
                    async_session, redis_client, "google", "invalid_token"
                )
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)


@pytest.mark.asyncio(loop_scope="session")
async def test_oauth_login_not_configured_fails(
    async_session: AsyncSession,
    redis_client: Redis,
):
    """Test that OAuth login fails when provider is not configured."""
    await cleanup_users(async_session)
    await cleanup_redis(redis_client)
    try:
        with patch(
            "app.services.auth._verify_google_token",
            side_effect=OAuthNotConfiguredError("google"),
        ):
            with pytest.raises(OAuthNotConfiguredError):
                await oauth_login(
                    async_session, redis_client, "google", "some_token"
                )
    finally:
        await cleanup_users(async_session)
        await cleanup_redis(redis_client)
