"""Tests for admin user management endpoints (ban, unban, reset AI scans, grant premium)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.utils.jwt import create_access_token, create_refresh_token
from app.utils.password import hash_password


async def _cleanup(session: AsyncSession) -> None:
    """Truncate relevant tables in dependency order."""
    await session.execute(text("TRUNCATE TABLE refresh_tokens CASCADE"))
    await session.execute(text("TRUNCATE TABLE users CASCADE"))
    await session.commit()


async def _create_user(
    session: AsyncSession,
    *,
    email: str,
    is_admin: bool = False,
    free_ai_scans_remaining: int = 5,
    subscription_status: str = "free",
) -> tuple[User, str]:
    """Create a test user and return (user, access_token)."""
    user = User(
        email=email,
        password_hash=hash_password("TestPass123"),
        is_admin=is_admin,
        free_ai_scans_remaining=free_ai_scans_remaining,
        subscription_status=subscription_status,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    token = create_access_token(str(user.id))
    return user, token


def _redis_key(jti: str) -> str:
    return f"refresh:{jti}"


BASE_URL = "/api/v1/admin/users"


@pytest.mark.asyncio(loop_scope="session")
class TestBanUser:
    """Test POST /admin/users/{user_id}/ban."""

    async def test_ban_user_sets_deleted_at(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Banning a user should set deleted_at to a non-null datetime."""
        await _cleanup(async_session)
        try:
            admin, admin_token = await _create_user(
                async_session, email="admin@ban-test.com", is_admin=True
            )
            target, _ = await _create_user(
                async_session, email="target@ban-test.com"
            )
            assert target.deleted_at is None

            response = await client.post(
                f"{BASE_URL}/{target.id}/ban",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["user_id"] == str(target.id)
            assert data["action"] == "ban"
            assert data["success"] is True

            await async_session.refresh(target)
            assert target.deleted_at is not None
        finally:
            await _cleanup(async_session)

    async def test_ban_user_revokes_refresh_tokens(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
        redis_client: Redis,
    ):
        """Banning a user should remove their refresh tokens from Redis."""
        await _cleanup(async_session)
        await redis_client.flushdb()
        try:
            admin, admin_token = await _create_user(
                async_session, email="admin@ban-token-test.com", is_admin=True
            )
            target, _ = await _create_user(
                async_session, email="target@ban-token-test.com"
            )

            # Simulate active refresh tokens in Redis
            _, jti1 = create_refresh_token(str(target.id))
            _, jti2 = create_refresh_token(str(target.id))
            await redis_client.set(_redis_key(jti1), str(target.id), ex=3600)
            await redis_client.set(_redis_key(jti2), str(target.id), ex=3600)

            # Also add a token for another user — should not be deleted
            _, other_jti = create_refresh_token(str(admin.id))
            await redis_client.set(_redis_key(other_jti), str(admin.id), ex=3600)

            response = await client.post(
                f"{BASE_URL}/{target.id}/ban",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert response.status_code == 200

            # Target's tokens should be gone
            assert await redis_client.get(_redis_key(jti1)) is None
            assert await redis_client.get(_redis_key(jti2)) is None

            # Admin's token should still exist
            assert await redis_client.get(_redis_key(other_jti)) == str(admin.id)
        finally:
            await redis_client.flushdb()
            await _cleanup(async_session)

    async def test_ban_user_not_found_returns_404(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Banning a non-existent user should return 404."""
        await _cleanup(async_session)
        try:
            _, admin_token = await _create_user(
                async_session, email="admin@ban-404-test.com", is_admin=True
            )
            fake_id = uuid4()
            response = await client.post(
                f"{BASE_URL}/{fake_id}/ban",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert response.status_code == 404
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestUnbanUser:
    """Test POST /admin/users/{user_id}/unban."""

    async def test_unban_user_clears_deleted_at(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Unbanning a user should set deleted_at back to None."""
        await _cleanup(async_session)
        try:
            _, admin_token = await _create_user(
                async_session, email="admin@unban-test.com", is_admin=True
            )
            target, _ = await _create_user(
                async_session, email="target@unban-test.com"
            )

            # Ban first
            target.deleted_at = datetime.now(UTC)
            await async_session.commit()
            await async_session.refresh(target)
            assert target.deleted_at is not None

            # Unban
            response = await client.post(
                f"{BASE_URL}/{target.id}/unban",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["action"] == "unban"
            assert data["success"] is True

            await async_session.refresh(target)
            assert target.deleted_at is None
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestResetAIScans:
    """Test POST /admin/users/{user_id}/reset-ai-scans."""

    async def test_reset_ai_scans_restores_quota(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Resetting AI scans should restore free_ai_scans_remaining to 5."""
        await _cleanup(async_session)
        try:
            _, admin_token = await _create_user(
                async_session, email="admin@reset-scan-test.com", is_admin=True
            )
            target, _ = await _create_user(
                async_session,
                email="target@reset-scan-test.com",
                free_ai_scans_remaining=0,
            )
            assert target.free_ai_scans_remaining == 0

            response = await client.post(
                f"{BASE_URL}/{target.id}/reset-ai-scans",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["action"] == "reset-ai-scans"
            assert data["success"] is True

            await async_session.refresh(target)
            assert target.free_ai_scans_remaining == 5
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestGrantPremium:
    """Test POST /admin/users/{user_id}/grant-premium."""

    async def test_grant_premium_sets_subscription_and_expiry(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Granting premium should set subscription_status and subscription_expires_at."""
        await _cleanup(async_session)
        try:
            _, admin_token = await _create_user(
                async_session, email="admin@premium-test.com", is_admin=True
            )
            target, _ = await _create_user(
                async_session, email="target@premium-test.com"
            )
            assert target.subscription_status == "free"
            assert target.subscription_expires_at is None

            before = datetime.now(UTC)
            response = await client.post(
                f"{BASE_URL}/{target.id}/grant-premium",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"days": 30},
            )
            after = datetime.now(UTC)

            assert response.status_code == 200
            data = response.json()
            assert data["action"] == "grant-premium"
            assert data["success"] is True

            await async_session.refresh(target)
            assert target.subscription_status == "premium"
            assert target.subscription_expires_at is not None

            # Verify expiry is approximately now + 30 days
            expected_min = before + timedelta(days=30)
            expected_max = after + timedelta(days=30)
            assert expected_min <= target.subscription_expires_at <= expected_max
        finally:
            await _cleanup(async_session)

    async def test_grant_premium_rejects_invalid_days(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Providing days < 1 should return 422 validation error."""
        await _cleanup(async_session)
        try:
            _, admin_token = await _create_user(
                async_session, email="admin@premium-invalid-test.com", is_admin=True
            )
            target, _ = await _create_user(
                async_session, email="target@premium-invalid-test.com"
            )
            response = await client.post(
                f"{BASE_URL}/{target.id}/grant-premium",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"days": 0},
            )
            assert response.status_code == 422
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestEndpointsRequireAdmin:
    """Test that all user management endpoints require admin privileges."""

    async def test_endpoints_require_admin_permission(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Non-admin users should get 403 for all user management endpoints."""
        await _cleanup(async_session)
        try:
            _, regular_token = await _create_user(
                async_session, email="regular@auth-test.com", is_admin=False
            )
            target, _ = await _create_user(
                async_session, email="target@auth-test.com"
            )

            endpoints = [
                f"{BASE_URL}/{target.id}/ban",
                f"{BASE_URL}/{target.id}/unban",
                f"{BASE_URL}/{target.id}/reset-ai-scans",
            ]

            for endpoint in endpoints:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {regular_token}"},
                )
                assert response.status_code == 403, f"Expected 403 for {endpoint}"

            # grant-premium requires body
            response = await client.post(
                f"{BASE_URL}/{target.id}/grant-premium",
                headers={"Authorization": f"Bearer {regular_token}"},
                json={"days": 30},
            )
            assert response.status_code == 403
        finally:
            await _cleanup(async_session)

    async def test_endpoints_require_authentication(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Unauthenticated requests should get 401."""
        fake_id = uuid4()
        endpoints = [
            f"{BASE_URL}/{fake_id}/ban",
            f"{BASE_URL}/{fake_id}/unban",
            f"{BASE_URL}/{fake_id}/reset-ai-scans",
            f"{BASE_URL}/{fake_id}/grant-premium",
        ]
        for endpoint in endpoints:
            response = await client.post(endpoint)
            assert response.status_code == 401, f"Expected 401 for {endpoint}"
