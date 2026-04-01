"""Shared fixtures for admin panel tests."""

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture(loop_scope="session")
async def admin_app(async_engine):
    """Create a minimal FastAPI app with SQLAdmin configured using test engine.

    Note: sqladmin adds its own SessionMiddleware internally when
    authentication_backend is provided, so we must NOT add another one here
    to avoid double-signing conflicts.
    """
    test_app = FastAPI()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.database.engine", async_engine)
        from app.admin.setup import setup_admin

        setup_admin(test_app)

    yield test_app


@pytest_asyncio.fixture(loop_scope="session")
async def admin_client(admin_app):
    """HTTP client for admin panel integration tests (unauthenticated)."""
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture(loop_scope="session")
async def authed_admin_client(admin_app):
    """HTTP client with authentication bypassed for view integration tests.

    Patches AdminAuth.authenticate to always return True so we can test
    view rendering without dealing with session cookies.
    Auth logic itself is covered in test_auth.py.
    """
    from unittest.mock import AsyncMock

    from app.admin.auth import AdminAuth

    original = AdminAuth.authenticate
    AdminAuth.authenticate = AsyncMock(return_value=True)
    try:
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
    finally:
        AdminAuth.authenticate = original
