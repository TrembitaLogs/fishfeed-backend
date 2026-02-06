"""Tests for the AdminAuth authentication backend."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import AdminAuth
from app.models.user import User
from app.utils.password import hash_password

TEST_PASSWORD = "Admin$ecure123"


async def _create_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    password: str = TEST_PASSWORD,
    is_admin: bool = True,
    deleted_at: datetime | None = None,
    password_hash: str | None = ...,
) -> User:
    """Helper: insert a User row and return it."""
    if password_hash is ...:
        password_hash = hash_password(password)
    user = User(
        id=uuid.uuid4(),
        email=email or f"admin-{uuid.uuid4().hex[:8]}@test.com",
        password_hash=password_hash,
        is_admin=is_admin,
        deleted_at=deleted_at,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def _make_request(*, session_data: dict | None = None) -> AsyncMock:
    """Build a fake Starlette Request with a mutable .session dict."""
    request = AsyncMock()
    request.session = dict(session_data or {})
    return request


def _make_login_request(email: str, password: str) -> AsyncMock:
    """Build a fake Starlette Request whose .form() returns login fields."""
    request = _make_request()
    form_data = {"username": email, "password": password}
    request.form = AsyncMock(return_value=form_data)
    return request


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM users WHERE email LIKE '%@test.com'"))
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
class TestAdminLogin:
    """AdminAuth.login() tests."""

    async def test_admin_login_valid_credentials(
        self, async_session: AsyncSession, async_engine
    ):
        user = await _create_user(async_session)
        try:
            request = _make_login_request(user.email, TEST_PASSWORD)

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            auth = AdminAuth(secret_key="test-secret")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                result = await auth.login(request)

            assert result is True
            assert request.session.get("admin_user_id") == str(user.id)
        finally:
            await _cleanup(async_session)

    async def test_admin_login_invalid_password(
        self, async_session: AsyncSession, async_engine
    ):
        user = await _create_user(async_session)
        try:
            request = _make_login_request(user.email, "WrongPassword999")

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            auth = AdminAuth(secret_key="test-secret")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                result = await auth.login(request)

            assert result is False
            assert "admin_user_id" not in request.session
        finally:
            await _cleanup(async_session)

    async def test_admin_login_non_admin_user(
        self, async_session: AsyncSession, async_engine
    ):
        user = await _create_user(async_session, is_admin=False)
        try:
            request = _make_login_request(user.email, TEST_PASSWORD)

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            auth = AdminAuth(secret_key="test-secret")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                result = await auth.login(request)

            assert result is False
            assert "admin_user_id" not in request.session
        finally:
            await _cleanup(async_session)

    async def test_admin_login_soft_deleted_user(
        self, async_session: AsyncSession, async_engine
    ):
        user = await _create_user(
            async_session,
            deleted_at=datetime.now(UTC),
        )
        try:
            request = _make_login_request(user.email, TEST_PASSWORD)

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            auth = AdminAuth(secret_key="test-secret")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                result = await auth.login(request)

            assert result is False
            assert "admin_user_id" not in request.session
        finally:
            await _cleanup(async_session)

    async def test_admin_login_oauth_only_user(
        self, async_session: AsyncSession, async_engine
    ):
        """OAuth-only users have no password_hash — login must be denied."""
        user = await _create_user(
            async_session, is_admin=True, password_hash=None
        )
        try:
            request = _make_login_request(user.email, TEST_PASSWORD)

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            auth = AdminAuth(secret_key="test-secret")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                result = await auth.login(request)

            assert result is False
        finally:
            await _cleanup(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestAdminLogout:
    """AdminAuth.logout() tests."""

    async def test_logout_clears_session(self):
        request = _make_request(session_data={"admin_user_id": "some-id"})
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.logout(request)

        assert result is True
        assert request.session == {}


@pytest.mark.asyncio(loop_scope="session")
class TestAdminAuthenticate:
    """AdminAuth.authenticate() tests."""

    async def test_authenticate_valid_session(
        self, async_session: AsyncSession, async_engine
    ):
        user = await _create_user(async_session)
        try:
            request = _make_request(
                session_data={"admin_user_id": str(user.id)}
            )

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            auth = AdminAuth(secret_key="test-secret")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                result = await auth.authenticate(request)

            assert result is True
        finally:
            await _cleanup(async_session)

    async def test_authenticate_no_session(self):
        request = _make_request()
        auth = AdminAuth(secret_key="test-secret")

        result = await auth.authenticate(request)

        assert result is False

    async def test_authenticate_deleted_admin(
        self, async_session: AsyncSession, async_engine
    ):
        """An admin who was soft-deleted after login should be rejected."""
        user = await _create_user(
            async_session,
            deleted_at=datetime.now(UTC),
        )
        try:
            request = _make_request(
                session_data={"admin_user_id": str(user.id)}
            )

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            auth = AdminAuth(secret_key="test-secret")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                result = await auth.authenticate(request)

            assert result is False
        finally:
            await _cleanup(async_session)

    async def test_authenticate_non_admin_in_session(
        self, async_session: AsyncSession, async_engine
    ):
        """If is_admin was revoked after login, authenticate must fail."""
        user = await _create_user(async_session, is_admin=False)
        try:
            request = _make_request(
                session_data={"admin_user_id": str(user.id)}
            )

            @asynccontextmanager
            async def _session_factory():
                yield async_session

            auth = AdminAuth(secret_key="test-secret")
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.admin.auth.async_session_maker", _session_factory
                )
                result = await auth.authenticate(request)

            assert result is False
        finally:
            await _cleanup(async_session)
