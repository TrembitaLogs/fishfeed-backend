"""Integration tests for the admin panel (Phase 4 of task 17 test strategy).

Covers:
- SQLAdmin UI loads after authentication
- API admin router paths remain accessible after refactor to package
- Session cookie security attributes
"""

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.utils.password import hash_password

TEST_PASSWORD = "Admin$ecure123"


async def _create_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    is_admin: bool = True,
) -> User:
    """Insert a user and return it."""
    user = User(
        id=uuid.uuid4(),
        email=email or f"integration-{uuid.uuid4().hex[:8]}@test.com",
        password_hash=hash_password(TEST_PASSWORD),
        is_admin=is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(
        text("DELETE FROM users WHERE email LIKE 'integration-%@test.com'")
    )
    await session.commit()


# ─── SQLAdmin UI loads ────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestSQLAdminUILoads:
    """Verify the admin panel renders correctly after authentication."""

    async def test_sqladmin_ui_loads_with_title(
        self,
        authed_admin_client: AsyncClient,
        async_session: AsyncSession,
        async_engine,
    ):
        """GET /admin/ after login should return HTML containing 'FishFeed Admin' title."""
        response = await authed_admin_client.get("/admin/")

        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert "FishFeed Admin" in response.text


# ─── Admin router paths unchanged after refactor ──────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestAdminRouterPathsUnchanged:
    """Verify all API admin paths remain registered after refactoring
    app/api/admin.py into app/api/admin/ package structure.

    Each request is sent without authentication on purpose — we only care
    that the route is NOT 404 (401 is expected for missing auth).
    """

    async def test_analytics_anonymize_path_exists(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        response = await client.post("/api/v1/admin/analytics/anonymize")
        assert response.status_code != 404

    async def test_analytics_cleanup_path_exists(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        response = await client.post("/api/v1/admin/analytics/cleanup")
        assert response.status_code != 404

    async def test_analytics_full_cleanup_path_exists(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        response = await client.post("/api/v1/admin/analytics/full-cleanup")
        assert response.status_code != 404

    async def test_dashboard_path_exists(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        response = await client.get("/api/v1/admin/dashboard")
        assert response.status_code != 404

    async def test_user_management_paths_exist(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        """All user management endpoints should be reachable (not 404)."""
        fake_id = uuid.uuid4()
        endpoints = [
            f"/api/v1/admin/users/{fake_id}/ban",
            f"/api/v1/admin/users/{fake_id}/unban",
            f"/api/v1/admin/users/{fake_id}/reset-ai-scans",
            f"/api/v1/admin/users/{fake_id}/grant-premium",
        ]
        for endpoint in endpoints:
            response = await client.post(endpoint)
            assert response.status_code != 404, f"Expected route to exist: {endpoint}"

    async def test_subscription_path_exists(
        self,
        client: AsyncClient,
        async_session: AsyncSession,
    ):
        fake_id = uuid.uuid4()
        response = await client.patch(
            f"/api/v1/admin/users/{fake_id}/subscription",
            json={"status": "free", "expires_at": None},
        )
        assert response.status_code != 404


# ─── Session cookie attributes ────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestSessionCookieAttributes:
    """Verify session cookie security attributes after admin login."""

    async def test_session_cookie_has_httponly(
        self,
        admin_app,
        async_session: AsyncSession,
        async_engine,
    ):
        """Login should set a session cookie with the HttpOnly flag."""
        user = await _create_user(async_session)
        try:

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                transport = ASGITransport(app=admin_app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/admin/login",
                        data={
                            "username": user.email,
                            "password": TEST_PASSWORD,
                        },
                        follow_redirects=False,
                    )

            # Successful login redirects to the admin dashboard
            assert response.status_code in (301, 302, 303, 307)

            set_cookie = response.headers.get("set-cookie", "")
            assert set_cookie, "Expected Set-Cookie header after login"

            set_cookie_lower = set_cookie.lower()
            assert "httponly" in set_cookie_lower, (
                f"Session cookie must be HttpOnly. Got: {set_cookie}"
            )
        finally:
            await _cleanup(async_session)

    async def test_session_cookie_has_samesite(
        self,
        admin_app,
        async_session: AsyncSession,
        async_engine,
    ):
        """Login should set a session cookie with the SameSite attribute."""
        user = await _create_user(async_session)
        try:

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                transport = ASGITransport(app=admin_app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/admin/login",
                        data={
                            "username": user.email,
                            "password": TEST_PASSWORD,
                        },
                        follow_redirects=False,
                    )

            assert response.status_code in (301, 302, 303, 307)

            set_cookie = response.headers.get("set-cookie", "")
            assert set_cookie, "Expected Set-Cookie header after login"

            set_cookie_lower = set_cookie.lower()
            assert "samesite=" in set_cookie_lower, (
                f"Session cookie must have SameSite attribute. Got: {set_cookie}"
            )
        finally:
            await _cleanup(async_session)
