"""Test fixtures for app/core/ — pure unit tests, no Redis or DB dependency."""

from collections.abc import AsyncGenerator

import pytest_asyncio


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def cleanup_auth_rate_limits() -> AsyncGenerator[None]:
    """Override the parent autouse fixture so core tests don't require Redis."""
    yield
