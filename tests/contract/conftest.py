"""Contract tests don't need a database or Redis.

The package-level conftest in tests/conftest.py declares an autouse fixture
(`cleanup_auth_rate_limits`) that requires a live Redis. Schema-only tests
shouldn't pay that tax, so we override the autouse fixture with a no-op for
this subdirectory.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def cleanup_auth_rate_limits() -> AsyncGenerator[None]:
    yield
