import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import RefreshToken, User


@pytest.mark.asyncio(loop_scope="session")
async def test_user_creation(async_session):
    """Test basic User creation with required fields."""
    user = User(
        email="test@example.com",
        password_hash="hashed_password",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    assert user.id is not None
    assert isinstance(user.id, uuid.UUID)
    assert user.email == "test@example.com"
    assert user.password_hash == "hashed_password"
    assert user.email_verified is False
    assert user.subscription_status == "free"
    assert user.free_ai_scans_remaining == 5


@pytest.mark.asyncio(loop_scope="session")
async def test_user_oauth_fields(async_session):
    """Test User with OAuth provider fields."""
    user = User(
        email="oauth@example.com",
        oauth_provider="google",
        oauth_id="google_123456",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    assert user.password_hash is None
    assert user.oauth_provider == "google"
    assert user.oauth_id == "google_123456"


@pytest.mark.asyncio(loop_scope="session")
async def test_user_settings_jsonb(async_session):
    """Test that JSONB settings field stores and retrieves JSON correctly."""
    settings = {
        "theme": "dark",
        "notifications": {"email": True, "push": False},
        "language": "uk",
    }
    user = User(
        email="settings@example.com",
        password_hash="hash",
        settings=settings,
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    assert user.settings == settings
    assert user.settings["theme"] == "dark"
    assert user.settings["notifications"]["email"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_user_default_settings(async_session):
    """Test that settings defaults to empty dict."""
    user = User(
        email="default_settings@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    assert user.settings == {}


@pytest.mark.asyncio(loop_scope="session")
async def test_user_has_timestamp_mixin(async_session):
    """Test that User has TimestampMixin columns."""
    user = User(
        email="timestamp@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    assert user.created_at is not None
    assert user.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_user_has_soft_delete_mixin(async_session):
    """Test that User has SoftDeleteMixin columns."""
    user = User(
        email="softdelete@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    assert user.deleted_at is None
    assert user.is_deleted() is False

    user.deleted_at = datetime.now(UTC)
    await async_session.commit()
    await async_session.refresh(user)

    assert user.is_deleted() is True


@pytest.mark.asyncio(loop_scope="session")
async def test_refresh_token_creation(async_session):
    """Test RefreshToken creation with User relationship."""
    user = User(
        email="token_user@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    token = RefreshToken(
        user_id=user.id,
        token_hash="token_hash_value",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    async_session.add(token)
    await async_session.commit()
    await async_session.refresh(token)

    assert token.id is not None
    assert token.user_id == user.id
    assert token.token_hash == "token_hash_value"
    assert token.revoked_at is None
    assert token.created_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_refresh_token_device_info_jsonb(async_session):
    """Test that device_info JSONB field stores and retrieves JSON correctly."""
    user = User(
        email="device_info_user@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    device_info = {
        "device_type": "mobile",
        "os": "iOS",
        "os_version": "17.0",
        "app_version": "1.0.0",
    }
    token = RefreshToken(
        user_id=user.id,
        token_hash="device_token_hash",
        expires_at=datetime.now(UTC) + timedelta(days=30),
        device_info=device_info,
    )
    async_session.add(token)
    await async_session.commit()
    await async_session.refresh(token)

    assert token.device_info == device_info
    assert token.device_info["device_type"] == "mobile"


@pytest.mark.asyncio(loop_scope="session")
async def test_refresh_token_user_relationship(async_session):
    """Test RefreshToken -> User relationship."""
    user = User(
        email="rel_user@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    token = RefreshToken(
        user_id=user.id,
        token_hash="rel_token_hash",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    async_session.add(token)
    await async_session.commit()
    await async_session.refresh(token)

    assert token.user.id == user.id
    assert token.user.email == "rel_user@example.com"


@pytest.mark.asyncio(loop_scope="session")
async def test_user_refresh_tokens_relationship(async_session):
    """Test User -> RefreshTokens relationship."""
    user = User(
        email="tokens_user@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    token1 = RefreshToken(
        user_id=user.id,
        token_hash="token1",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    token2 = RefreshToken(
        user_id=user.id,
        token_hash="token2",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    async_session.add_all([token1, token2])
    await async_session.commit()

    # Reload user with refresh_tokens eagerly loaded
    result = await async_session.execute(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.refresh_tokens))
    )
    user = result.scalar_one()

    assert len(user.refresh_tokens) == 2
    token_hashes = {t.token_hash for t in user.refresh_tokens}
    assert token_hashes == {"token1", "token2"}


@pytest.mark.asyncio(loop_scope="session")
async def test_user_cascade_delete_tokens(async_session):
    """Test that deleting User cascades to RefreshTokens."""
    user = User(
        email="cascade_user@example.com",
        password_hash="hash",
    )
    async_session.add(user)
    await async_session.commit()
    await async_session.refresh(user)

    token = RefreshToken(
        user_id=user.id,
        token_hash="cascade_token",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    async_session.add(token)
    await async_session.commit()
    token_id = token.id

    await async_session.delete(user)
    await async_session.commit()

    deleted_token = await async_session.get(RefreshToken, token_id)
    assert deleted_token is None
