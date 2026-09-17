"""Tests for PATCH /admin/users/{user_id}/subscription endpoint."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.utils.jwt import create_access_token
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
    subscription_status: str = "free",
    subscription_expires_at: datetime | None = None,
) -> tuple[User, str]:
    """Create a test user and return (user, access_token)."""
    user = User(
        email=email,
        password_hash=hash_password("TestPass123"),
        is_admin=is_admin,
        subscription_status=subscription_status,
        subscription_expires_at=subscription_expires_at,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    token = create_access_token(str(user.id))
    return user, token


BASE_URL = "/api/v1/admin/users"


def test_subscription_update_openapi_declares_revenuecat_conflict(app) -> None:
    responses = app.openapi()["paths"]["/api/v1/admin/users/{user_id}/subscription"]["patch"]["responses"]
    assert responses["409"]["description"] == "Manage premium grants and revocations in RevenueCat"


@pytest.mark.asyncio(loop_scope="session")
class TestUpdateSubscription:
    """Test PATCH /admin/users/{user_id}/subscription is read-only."""

    async def test_update_subscription_is_rejected_without_mutating_user(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Subscription projections must be managed by RevenueCat."""
        await _cleanup(async_session)
        try:
            _, admin_token = await _create_user(async_session, email="admin@sub-premium-test.com", is_admin=True)
            target, _ = await _create_user(async_session, email="target@sub-premium-test.com")
            assert target.subscription_status == "free"
            assert target.subscription_expires_at is None

            expires = datetime.now(UTC) + timedelta(days=30)
            response = await client.patch(
                f"{BASE_URL}/{target.id}/subscription",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "status": "premium",
                    "expires_at": expires.isoformat(),
                },
            )
            assert response.status_code == 409
            assert response.json()["detail"] == "Manage premium grants and revocations in RevenueCat"

            await async_session.refresh(target)
            assert target.subscription_status == "free"
            assert target.subscription_expires_at is None
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.asyncio(loop_scope="session")
class TestUpdateSubscriptionValidation:
    """Test validation for subscription update endpoint."""

    async def test_invalid_status_returns_422(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Providing an invalid status value should return 422."""
        await _cleanup(async_session)
        try:
            _, admin_token = await _create_user(async_session, email="admin@sub-invalid-test.com", is_admin=True)
            target, _ = await _create_user(async_session, email="target@sub-invalid-test.com")

            response = await client.patch(
                f"{BASE_URL}/{target.id}/subscription",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "status": "gold",
                    "expires_at": None,
                },
            )
            assert response.status_code == 422
        finally:
            await _cleanup(async_session)

    async def test_user_not_found_returns_404(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Updating subscription for non-existent user should return 404."""
        await _cleanup(async_session)
        try:
            _, admin_token = await _create_user(async_session, email="admin@sub-404-test.com", is_admin=True)
            fake_id = uuid4()
            response = await client.patch(
                f"{BASE_URL}/{fake_id}/subscription",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "status": "premium",
                    "expires_at": None,
                },
            )
            assert response.status_code == 404
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestSubscriptionRequiresAdmin:
    """Test that subscription endpoint requires admin privileges."""

    async def test_requires_admin_permission(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """Non-admin user should get 403."""
        await _cleanup(async_session)
        try:
            _, regular_token = await _create_user(async_session, email="regular@sub-auth-test.com", is_admin=False)
            target, _ = await _create_user(async_session, email="target@sub-auth-test.com")

            response = await client.patch(
                f"{BASE_URL}/{target.id}/subscription",
                headers={"Authorization": f"Bearer {regular_token}"},
                json={"status": "premium", "expires_at": None},
            )
            assert response.status_code == 403
        finally:
            await _cleanup(async_session)

    async def test_requires_authentication(
        self,
        client: AsyncClient,
    ):
        """Unauthenticated request should get 401."""
        fake_id = uuid4()
        response = await client.patch(
            f"{BASE_URL}/{fake_id}/subscription",
            json={"status": "premium", "expires_at": None},
        )
        assert response.status_code == 401
