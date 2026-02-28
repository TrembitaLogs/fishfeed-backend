"""E2E test fixtures for image sync integration tests.

These tests require a running MinIO instance at localhost:9000
(started via docker-compose). If MinIO is unreachable, all E2E
tests in this directory are skipped automatically.
"""

import os
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import aioboto3
import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

# MinIO settings for E2E tests (matching docker-compose defaults)
E2E_S3_ENDPOINT = "http://localhost:9000"
E2E_S3_ACCESS_KEY = "minioadmin"
E2E_S3_SECRET_KEY = "minioadmin"
E2E_S3_REGION = "us-east-1"
E2E_S3_BUCKET = "fishfeed-images"

# Env overrides applied during E2E session (restored on teardown)
_S3_ENV_OVERRIDES = {
    "S3_ENDPOINT_URL": E2E_S3_ENDPOINT,
    "S3_PRESIGNED_ENDPOINT_URL": E2E_S3_ENDPOINT,
    "S3_ACCESS_KEY": E2E_S3_ACCESS_KEY,
    "S3_SECRET_KEY": E2E_S3_SECRET_KEY,
    "S3_REGION": E2E_S3_REGION,
    "S3_IMAGES_BUCKET_NAME": E2E_S3_BUCKET,
}


def _build_e2e_settings() -> MagicMock:
    """Build a mock Settings object with S3 pointing to local MinIO.

    Only used for the cleanup job where ``settings`` is captured at
    module-import time and can't be refreshed via ``get_settings()``.
    """
    real = get_settings()
    mock = MagicMock()
    # Copy all real attributes
    for attr in dir(real):
        if attr.startswith("_"):
            continue
        try:
            setattr(mock, attr, getattr(real, attr))
        except (AttributeError, TypeError):
            pass
    # Override S3 fields
    mock.S3_ENDPOINT_URL = E2E_S3_ENDPOINT
    mock.S3_PRESIGNED_ENDPOINT_URL = E2E_S3_ENDPOINT
    mock.S3_ACCESS_KEY = E2E_S3_ACCESS_KEY
    mock.S3_SECRET_KEY = E2E_S3_SECRET_KEY
    mock.S3_REGION = E2E_S3_REGION
    mock.S3_IMAGES_BUCKET_NAME = E2E_S3_BUCKET
    return mock


@pytest.fixture(scope="session", autouse=True)
def minio_available():
    """Skip all E2E tests if MinIO is not reachable."""
    try:
        resp = httpx.get(f"{E2E_S3_ENDPOINT}/minio/health/live", timeout=3)
        if resp.status_code != 200:
            pytest.skip("MinIO health check failed")
    except (httpx.ConnectError, httpx.TimeoutException):
        pytest.skip("MinIO not reachable at localhost:9000")


@pytest.fixture(scope="session", autouse=True)
def override_s3_env():
    """Set S3 env vars and clear get_settings cache for the entire session.

    This is the most reliable approach: all modules that call
    ``get_settings()`` (including transitive calls in image_service,
    image_cleanup, etc.) will get a real ``Settings`` instance with
    the correct S3 values — no mock attribute problems.
    """
    original_env: dict[str, str | None] = {}
    for key, value in _S3_ENV_OVERRIDES.items():
        original_env[key] = os.environ.get(key)
        os.environ[key] = value

    # Clear lru_cache so next get_settings() picks up new env vars
    get_settings.cache_clear()

    yield

    # Restore original env vars
    for key, orig_value in original_env.items():
        if orig_value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig_value
    get_settings.cache_clear()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def ensure_bucket():
    """Ensure the test bucket exists in MinIO (created once per session)."""
    session = aioboto3.Session()
    config = {
        "service_name": "s3",
        "endpoint_url": E2E_S3_ENDPOINT,
        "aws_access_key_id": E2E_S3_ACCESS_KEY,
        "aws_secret_access_key": E2E_S3_SECRET_KEY,
        "region_name": E2E_S3_REGION,
    }
    async with session.client(**config) as s3:
        try:
            await s3.head_bucket(Bucket=E2E_S3_BUCKET)
        except Exception:
            await s3.create_bucket(Bucket=E2E_S3_BUCKET)


@pytest_asyncio.fixture(loop_scope="session")
async def s3_client(ensure_bucket) -> AsyncGenerator[object]:
    """Provide a real S3 client connected to local MinIO for verification."""
    session = aioboto3.Session()
    config = {
        "service_name": "s3",
        "endpoint_url": E2E_S3_ENDPOINT,
        "aws_access_key_id": E2E_S3_ACCESS_KEY,
        "aws_secret_access_key": E2E_S3_SECRET_KEY,
        "region_name": E2E_S3_REGION,
    }
    async with session.client(**config) as client:
        yield client


@pytest_asyncio.fixture(loop_scope="session")
async def s3_cleanup(s3_client) -> AsyncGenerator[list[str]]:
    """Track S3 keys created during a test and delete them in teardown.

    Usage in test:
        s3_cleanup.append("aquariums/xyz/abc.webp")
    """
    created_keys: list[str] = []
    yield created_keys
    for key in created_keys:
        try:
            await s3_client.delete_object(Bucket=E2E_S3_BUCKET, Key=key)
        except Exception:
            pass  # Best-effort cleanup


@pytest_asyncio.fixture(loop_scope="session")
async def cleanup_e2e_data(async_session: AsyncSession) -> AsyncGenerator[None]:
    """Clean up all E2E test data before and after each test."""
    from sqlalchemy import text

    async def _cleanup() -> None:
        await async_session.execute(text("DELETE FROM orphaned_images"))
        await async_session.execute(text("DELETE FROM fish"))
        await async_session.execute(text("DELETE FROM aquarium_members"))
        await async_session.execute(text("DELETE FROM aquariums"))
        # Do not delete users unconditionally — only test users
        await async_session.execute(
            text("DELETE FROM users WHERE email LIKE 'e2e-%@example.com'"),
        )
        await async_session.commit()

    await _cleanup()
    yield
    await _cleanup()
