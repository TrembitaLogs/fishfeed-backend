"""Integration tests for the admin panel.

Covers:
- SQLAdmin UI loads after authentication
- API admin router paths remain accessible after refactor to package
- Session cookie security attributes
- Session persistence across requests
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.password import hash_password

TEST_USERNAME = "admin"
TEST_PASSWORD = "Admin$ecure123"


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

    async def test_session_cookie_has_httponly(self, admin_app, monkeypatch):
        """Login should set a session cookie with the HttpOnly flag."""
        monkeypatch.setattr("app.admin.auth.get_settings", lambda: _settings())

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/admin/login",
                data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
                follow_redirects=False,
            )

        assert response.status_code in (301, 302, 303, 307)

        set_cookie = response.headers.get("set-cookie", "")
        assert set_cookie, "Expected Set-Cookie header after login"

        set_cookie_lower = set_cookie.lower()
        assert "httponly" in set_cookie_lower, (
            f"Session cookie must be HttpOnly. Got: {set_cookie}"
        )

    async def test_session_cookie_has_samesite(self, admin_app, monkeypatch):
        """Login should set a session cookie with the SameSite attribute."""
        monkeypatch.setattr("app.admin.auth.get_settings", lambda: _settings())

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/admin/login",
                data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
                follow_redirects=False,
            )

        assert response.status_code in (301, 302, 303, 307)

        set_cookie = response.headers.get("set-cookie", "")
        assert set_cookie, "Expected Set-Cookie header after login"

        set_cookie_lower = set_cookie.lower()
        assert "samesite=" in set_cookie_lower, (
            f"Session cookie must have SameSite attribute. Got: {set_cookie}"
        )


# ─── Session persistence ─────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
class TestSessionPersistence:
    """Verify admin sessions persist across requests via SessionMiddleware."""

    async def test_session_persists_after_login(self, admin_app, monkeypatch):
        """After login, subsequent requests should remain authenticated."""
        monkeypatch.setattr("app.admin.auth.get_settings", lambda: _settings())

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            login_response = await client.post(
                "/admin/login",
                data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
                follow_redirects=False,
            )
            assert login_response.status_code in (301, 302, 303, 307)

            session_cookie = login_response.cookies.get("session")
            assert session_cookie, "Expected session cookie after login"

            admin_response = await client.get("/admin/", follow_redirects=False)

            assert admin_response.status_code == 200
            assert "text/html" in admin_response.headers.get("content-type", "")

    async def test_unauthenticated_request_redirects_to_login(self, admin_app):
        """Without a session cookie, /admin/ should redirect to login."""
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get("/admin/", follow_redirects=False)

        assert response.status_code in (301, 302, 303, 307)
        assert "/admin/login" in response.headers.get("location", "")

    async def test_logout_invalidates_session(self, admin_app, monkeypatch):
        """After logout, the session should no longer grant access."""
        monkeypatch.setattr("app.admin.auth.get_settings", lambda: _settings())

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            await client.post(
                "/admin/login",
                data={"username": TEST_USERNAME, "password": TEST_PASSWORD},
                follow_redirects=False,
            )

            await client.get("/admin/logout", follow_redirects=False)

            response = await client.get("/admin/", follow_redirects=False)

        assert response.status_code in (301, 302, 303, 307)
        assert "/admin/login" in response.headers.get("location", "")


# ─── Helpers ─────────────────────────────────────────────────────────


class _FakeSettings:
    def __init__(self, username: str, password: str):
        self.ADMIN_USERNAME = username
        self.ADMIN_PASSWORD = SecretStr(password)


def _settings(
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
) -> _FakeSettings:
    hashed = hash_password(password) if password else password
    return _FakeSettings(username=username, password=hashed)
