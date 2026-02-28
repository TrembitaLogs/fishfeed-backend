"""Tests for auth dependencies."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dependencies import (
    check_image_upload_rate_limit,
    get_current_active_user,
    get_current_user,
    require_premium,
)
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
        mock_user.token_version = 0

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


class TestCheckImageUploadRateLimit:
    """Tests for check_image_upload_rate_limit dependency."""

    def _make_user(self) -> MagicMock:
        mock_user = MagicMock(spec=User)
        mock_user.id = uuid4()
        mock_user.deleted_at = None
        return mock_user

    def _make_redis(self, current_count: int, ttl: int = 45) -> AsyncMock:
        mock_redis = AsyncMock()
        mock_pipe = MagicMock()
        mock_pipe.incr = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(return_value=[current_count, True])
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        mock_redis.ttl.return_value = ttl
        return mock_redis

    @pytest.mark.asyncio
    async def test_first_request_passes(self):
        """Test that first upload request passes rate limit check."""
        user = self._make_user()
        redis = self._make_redis(current_count=1)

        result = await check_image_upload_rate_limit(current_user=user, redis=redis)

        assert result is None
        redis.pipeline.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_within_limit_passes(self):
        """Test that requests within limit pass."""
        user = self._make_user()
        redis = self._make_redis(current_count=20)

        result = await check_image_upload_rate_limit(current_user=user, redis=redis)

        assert result is None

    @pytest.mark.asyncio
    async def test_request_exceeding_limit_raises_429(self):
        """Test that exceeding rate limit raises HTTPException 429."""
        user = self._make_user()
        redis = self._make_redis(current_count=21, ttl=30)

        with pytest.raises(HTTPException) as exc_info:
            await check_image_upload_rate_limit(current_user=user, redis=redis)

        assert exc_info.value.status_code == 429
        assert exc_info.value.detail == "Image upload rate limit exceeded"
        assert exc_info.value.headers["Retry-After"] == "30"

    @pytest.mark.asyncio
    async def test_retry_after_header_minimum_1_second(self):
        """Test that Retry-After header is at least 1 second."""
        user = self._make_user()
        redis = self._make_redis(current_count=21, ttl=-1)

        with pytest.raises(HTTPException) as exc_info:
            await check_image_upload_rate_limit(current_user=user, redis=redis)

        assert exc_info.value.headers["Retry-After"] == "1"

    @pytest.mark.asyncio
    async def test_pipeline_uses_correct_key(self):
        """Test that Redis pipeline uses correct key with prefix."""
        user = self._make_user()
        redis = self._make_redis(current_count=1)

        await check_image_upload_rate_limit(current_user=user, redis=redis)

        pipe = redis.pipeline.return_value
        expected_key = f"fishfeed:image_upload:{user.id}"
        pipe.incr.assert_called_once_with(expected_key)
        pipe.expire.assert_called_once_with(expected_key, 60)

    @pytest.mark.asyncio
    async def test_applies_to_all_users_equally(self):
        """Test that rate limit applies to all users (no premium bypass)."""
        user = self._make_user()
        user.subscription_status = "premium"
        redis = self._make_redis(current_count=21, ttl=30)

        with pytest.raises(HTTPException) as exc_info:
            await check_image_upload_rate_limit(current_user=user, redis=redis)

        assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_custom_rate_limit_setting(self):
        """Test that rate limit respects RATE_LIMIT_IMAGE_UPLOAD_PER_MIN setting."""
        user = self._make_user()
        redis = self._make_redis(current_count=6)

        with patch("app.dependencies.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.REDIS_KEY_PREFIX = "fishfeed:"
            mock_settings.RATE_LIMIT_IMAGE_UPLOAD_PER_MIN = 5
            mock_get_settings.return_value = mock_settings

            with pytest.raises(HTTPException) as exc_info:
                await check_image_upload_rate_limit(current_user=user, redis=redis)

            assert exc_info.value.status_code == 429
