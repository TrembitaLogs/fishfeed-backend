"""Tests for the AdminAuth authentication backend (env-based credentials)."""

from unittest.mock import AsyncMock

import pytest

from app.admin.auth import AdminAuth

TEST_USERNAME = "admin"
TEST_PASSWORD = "Admin$ecure123"


def _make_request(*, session_data: dict | None = None) -> AsyncMock:
    """Build a fake Starlette Request with a mutable .session dict."""
    request = AsyncMock()
    request.session = dict(session_data or {})
    return request


def _make_login_request(username: str, password: str) -> AsyncMock:
    """Build a fake Starlette Request whose .form() returns login fields."""
    request = _make_request()
    form_data = {"username": username, "password": password}
    request.form = AsyncMock(return_value=form_data)
    return request


@pytest.mark.asyncio(loop_scope="session")
class TestAdminLogin:
    """AdminAuth.login() tests."""

    async def test_login_valid_credentials(self, monkeypatch):
        monkeypatch.setattr("app.admin.auth.get_settings", lambda: _settings())
        request = _make_login_request(TEST_USERNAME, TEST_PASSWORD)
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.login(request)

        assert result is True
        assert request.session.get("admin") is True

    async def test_login_wrong_password(self, monkeypatch):
        monkeypatch.setattr("app.admin.auth.get_settings", lambda: _settings())
        request = _make_login_request(TEST_USERNAME, "WrongPassword999")
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.login(request)

        assert result is False
        assert "admin" not in request.session

    async def test_login_wrong_username(self, monkeypatch):
        monkeypatch.setattr("app.admin.auth.get_settings", lambda: _settings())
        request = _make_login_request("wrong-user", TEST_PASSWORD)
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.login(request)

        assert result is False
        assert "admin" not in request.session

    async def test_login_empty_form_fields(self, monkeypatch):
        monkeypatch.setattr("app.admin.auth.get_settings", lambda: _settings())
        request = _make_login_request("", "")
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.login(request)

        assert result is False

    async def test_login_credentials_not_configured(self, monkeypatch):
        """When ADMIN_USERNAME/PASSWORD are empty, login is always rejected."""
        monkeypatch.setattr(
            "app.admin.auth.get_settings",
            lambda: _settings(username="", password=""),
        )
        request = _make_login_request(TEST_USERNAME, TEST_PASSWORD)
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.login(request)

        assert result is False


@pytest.mark.asyncio(loop_scope="session")
class TestAdminLogout:
    """AdminAuth.logout() tests."""

    async def test_logout_clears_session(self):
        request = _make_request(session_data={"admin": True})
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.logout(request)

        assert result is True
        assert request.session == {}


@pytest.mark.asyncio(loop_scope="session")
class TestAdminAuthenticate:
    """AdminAuth.authenticate() tests."""

    async def test_authenticate_valid_session(self):
        request = _make_request(session_data={"admin": True})
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.authenticate(request)

        assert result is True

    async def test_authenticate_no_session(self):
        request = _make_request()
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.authenticate(request)

        assert result is False

    async def test_authenticate_invalid_session_value(self):
        """Session with admin=False should be rejected."""
        request = _make_request(session_data={"admin": False})
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.authenticate(request)

        assert result is False


# ─── Helpers ─────────────────────────────────────────────────────────


class _FakeSettings:
    def __init__(self, username: str, password: str):
        self.ADMIN_USERNAME = username
        self.ADMIN_PASSWORD = password


def _settings(
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
) -> _FakeSettings:
    return _FakeSettings(username=username, password=password)
