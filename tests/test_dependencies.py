"""Tests for auth dependencies."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dependencies import get_current_active_user, get_current_user, require_premium
from app.models.user import User
from app.utils.jwt import create_access_token, create_refresh_token


class TestGetCurrentUser:
    """Tests for get_current_user dependency."""

    @pytest.mark.asyncio
    async def test_valid_token_returns_user(self):
        """Test that get_current_user with valid token returns User."""
        user_id = uuid4()
        token = create_access_token(user_id)

        mock_user = MagicMock(spec=User)
        mock_user.id = user_id
        mock_user.email = "test@example.com"
        mock_user.deleted_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        result = await get_current_user(token=token, db=mock_db)

        assert result == mock_user
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_token_raises_401(self):
        """Test that get_current_user with invalid token raises 401."""
        mock_db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token="invalid.token.here", db=mock_db)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Could not validate credentials"

    @pytest.mark.asyncio
    async def test_expired_token_raises_401(self):
        """Test that get_current_user with expired token raises 401."""
        user_id = uuid4()
        token = create_access_token(user_id, expires_delta=timedelta(seconds=-1))

        mock_db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token=token, db=mock_db)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Could not validate credentials"

    @pytest.mark.asyncio
    async def test_refresh_token_type_raises_401(self):
        """Test that get_current_user rejects refresh token type."""
        user_id = uuid4()
        token, _ = create_refresh_token(user_id)

        mock_db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token=token, db=mock_db)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Could not validate credentials"

    @pytest.mark.asyncio
    async def test_user_not_found_raises_401(self):
        """Test that get_current_user raises 401 when user not in database."""
        user_id = uuid4()
        token = create_access_token(user_id)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token=token, db=mock_db)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Could not validate credentials"

    @pytest.mark.asyncio
    async def test_tampered_token_raises_401(self):
        """Test that get_current_user with tampered token raises 401."""
        user_id = uuid4()
        token = create_access_token(user_id)
        tampered_token = token[:-5] + "xxxxx"

        mock_db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token=tampered_token, db=mock_db)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Could not validate credentials"

    @pytest.mark.asyncio
    async def test_www_authenticate_header_present(self):
        """Test that 401 response includes WWW-Authenticate header."""
        mock_db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token="invalid", db=mock_db)

        assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


class TestGetCurrentActiveUser:
    """Tests for get_current_active_user dependency."""

    @pytest.mark.asyncio
    async def test_active_user_returns_user(self):
        """Test that get_current_active_user with active user returns User."""
        mock_user = MagicMock(spec=User)
        mock_user.deleted_at = None

        result = await get_current_active_user(current_user=mock_user)

        assert result == mock_user

    @pytest.mark.asyncio
    async def test_deleted_user_raises_403(self):
        """Test that get_current_active_user with deleted user raises 403."""
        mock_user = MagicMock(spec=User)
        mock_user.deleted_at = datetime.now(UTC)

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(current_user=mock_user)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "User account is deactivated"


class TestRequirePremium:
    """Tests for require_premium dependency."""

    @pytest.mark.asyncio
    async def test_premium_user_returns_user(self):
        """Test that require_premium with premium user returns User."""
        mock_user = MagicMock(spec=User)
        mock_user.id = uuid4()
        mock_user.subscription_status = "premium"
        mock_user.subscription_expires_at = datetime.now(UTC) + timedelta(days=30)
        mock_user.deleted_at = None

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True

        result = await require_premium(current_user=mock_user, redis=mock_redis)

        assert result == mock_user

    @pytest.mark.asyncio
    async def test_premium_user_without_expiry_returns_user(self):
        """Test that require_premium with premium user without expiry returns User."""
        mock_user = MagicMock(spec=User)
        mock_user.id = uuid4()
        mock_user.subscription_status = "premium"
        mock_user.subscription_expires_at = None
        mock_user.deleted_at = None

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True

        result = await require_premium(current_user=mock_user, redis=mock_redis)

        assert result == mock_user

    @pytest.mark.asyncio
    async def test_free_user_raises_403(self):
        """Test that require_premium with free user raises 403."""
        mock_user = MagicMock(spec=User)
        mock_user.id = uuid4()
        mock_user.subscription_status = "free"
        mock_user.subscription_expires_at = None
        mock_user.deleted_at = None

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await require_premium(current_user=mock_user, redis=mock_redis)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Premium subscription required"

    @pytest.mark.asyncio
    async def test_expired_subscription_raises_403(self):
        """Test that require_premium with expired subscription raises 403."""
        mock_user = MagicMock(spec=User)
        mock_user.id = uuid4()
        mock_user.subscription_status = "premium"
        mock_user.subscription_expires_at = datetime.now(UTC) - timedelta(days=1)
        mock_user.deleted_at = None

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await require_premium(current_user=mock_user, redis=mock_redis)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Premium subscription required"

    @pytest.mark.asyncio
    async def test_uses_cached_premium_status(self):
        """Test that require_premium uses cached premium status."""
        mock_user = MagicMock(spec=User)
        mock_user.id = uuid4()
        mock_user.subscription_status = "free"  # User is actually free
        mock_user.subscription_expires_at = None
        mock_user.deleted_at = None

        mock_redis = AsyncMock()
        # Cache says user is premium
        mock_redis.get.return_value = '{"is_premium": true}'

        # Should not raise because cache says premium
        result = await require_premium(current_user=mock_user, redis=mock_redis)

        assert result == mock_user
