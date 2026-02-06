"""Tests for admin API endpoints."""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import AnalyticsEvent
from app.models.user import User
from app.utils.jwt import create_access_token
from app.utils.password import hash_password


async def cleanup_admin_data(session: AsyncSession) -> None:
    """Helper to cleanup admin test data."""
    await session.execute(text("DELETE FROM analytics_events"))
    await session.execute(text("DELETE FROM users WHERE email LIKE '%admin_test%'"))
    await session.commit()


async def create_test_user(
    session: AsyncSession,
    email: str,
    is_admin: bool = False,
) -> tuple[User, str]:
    """Helper to create a test user and return user with access token."""
    user = User(
        email=email,
        password_hash=hash_password("TestPass123"),
        is_admin=is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    access_token = create_access_token(str(user.id))
    return user, access_token


async def create_analytics_event(
    session: AsyncSession,
    user_id: uuid.UUID | None = None,
    event_type: str = "test_event",
    properties: dict | None = None,
    ip_hash: str = "a" * 64,
    created_at: datetime | None = None,
    anonymized_at: datetime | None = None,
) -> AnalyticsEvent:
    """Helper to create a test analytics event."""
    event = AnalyticsEvent(
        user_id=user_id,
        event_type=event_type,
        properties=properties or {},
        ip_hash=ip_hash,
        created_at=created_at or datetime.now(UTC),
        anonymized_at=anonymized_at,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


@pytest.mark.asyncio(loop_scope="session")
class TestAdminAnalyticsAnonymize:
    """Tests for POST /admin/analytics/anonymize endpoint."""

    async def test_anonymize_requires_admin(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that anonymize endpoint requires admin privileges."""
        await cleanup_admin_data(async_session)
        try:
            # Create non-admin user
            _, token = await create_test_user(
                async_session, "non_admin_test@example.com", is_admin=False
            )

            response = await client.post(
                "/api/v1/admin/analytics/anonymize",
                headers={"Authorization": f"Bearer {token}"},
            )

            assert response.status_code == 403
            assert "Admin privileges required" in response.json()["detail"]
        finally:
            await cleanup_admin_data(async_session)

    async def test_anonymize_requires_authentication(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that anonymize endpoint requires authentication."""
        response = await client.post("/api/v1/admin/analytics/anonymize")

        assert response.status_code == 401

    async def test_anonymize_dry_run(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test anonymize dry-run mode returns correct info."""
        await cleanup_admin_data(async_session)
        try:
            admin, token = await create_test_user(
                async_session, "admin_test@example.com", is_admin=True
            )

            # Mock the job function to avoid database session issues
            mock_result = {
                "job": "anonymize_old_events",
                "dry_run": True,
                "cutoff_date": "2025-12-15T00:00:00+00:00",
                "anonymize_after_days": 30,
                "batch_size": 1000,
                "total_anonymized": 0,
                "would_anonymize": 5,
                "batches_processed": 0,
            }

            with patch(
                "app.api.admin.analytics.anonymize_old_events_job", return_value=mock_result
            ):
                response = await client.post(
                    "/api/v1/admin/analytics/anonymize",
                    params={"dry_run": True},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["dry_run"] is True
            assert data["job"] == "anonymize_old_events"
        finally:
            await cleanup_admin_data(async_session)

    async def test_anonymize_runs_job(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that anonymize endpoint runs the job successfully."""
        await cleanup_admin_data(async_session)
        try:
            admin, token = await create_test_user(
                async_session, "admin_test@example.com", is_admin=True
            )

            mock_result = {
                "job": "anonymize_old_events",
                "dry_run": False,
                "cutoff_date": "2025-12-15T00:00:00+00:00",
                "anonymize_after_days": 30,
                "batch_size": 1000,
                "total_anonymized": 10,
                "would_anonymize": 0,
                "batches_processed": 1,
            }

            with patch(
                "app.api.admin.analytics.anonymize_old_events_job", return_value=mock_result
            ):
                response = await client.post(
                    "/api/v1/admin/analytics/anonymize",
                    params={"dry_run": False},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["dry_run"] is False
            assert "cutoff_date" in data
        finally:
            await cleanup_admin_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestAdminAnalyticsCleanup:
    """Tests for POST /admin/analytics/cleanup endpoint."""

    async def test_cleanup_requires_admin(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that cleanup endpoint requires admin privileges."""
        await cleanup_admin_data(async_session)
        try:
            # Create non-admin user
            _, token = await create_test_user(
                async_session, "non_admin_test@example.com", is_admin=False
            )

            response = await client.post(
                "/api/v1/admin/analytics/cleanup",
                headers={"Authorization": f"Bearer {token}"},
            )

            assert response.status_code == 403
        finally:
            await cleanup_admin_data(async_session)

    async def test_cleanup_dry_run(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test cleanup dry-run mode returns correct info."""
        await cleanup_admin_data(async_session)
        try:
            admin, token = await create_test_user(
                async_session, "admin_test@example.com", is_admin=True
            )

            mock_result = {
                "job": "delete_old_events",
                "dry_run": True,
                "cutoff_date": "2025-10-15T00:00:00+00:00",
                "retention_days": 90,
                "batch_size": 1000,
                "total_deleted": 0,
                "would_delete": 3,
                "batches_processed": 0,
            }

            with patch(
                "app.api.admin.analytics.delete_old_events_job", return_value=mock_result
            ):
                response = await client.post(
                    "/api/v1/admin/analytics/cleanup",
                    params={"dry_run": True},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["dry_run"] is True
            assert data["job"] == "delete_old_events"
        finally:
            await cleanup_admin_data(async_session)

    async def test_cleanup_runs_job(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that cleanup endpoint runs the job successfully."""
        await cleanup_admin_data(async_session)
        try:
            admin, token = await create_test_user(
                async_session, "admin_test@example.com", is_admin=True
            )

            mock_result = {
                "job": "delete_old_events",
                "dry_run": False,
                "cutoff_date": "2025-10-15T00:00:00+00:00",
                "retention_days": 90,
                "batch_size": 1000,
                "total_deleted": 5,
                "would_delete": 0,
                "batches_processed": 1,
            }

            with patch(
                "app.api.admin.analytics.delete_old_events_job", return_value=mock_result
            ):
                response = await client.post(
                    "/api/v1/admin/analytics/cleanup",
                    params={"dry_run": False},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["dry_run"] is False
            assert "cutoff_date" in data
        finally:
            await cleanup_admin_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
class TestAdminAnalyticsFullCleanup:
    """Tests for POST /admin/analytics/full-cleanup endpoint."""

    async def test_full_cleanup_requires_admin(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that full-cleanup endpoint requires admin privileges."""
        await cleanup_admin_data(async_session)
        try:
            # Create non-admin user
            _, token = await create_test_user(
                async_session, "non_admin_test@example.com", is_admin=False
            )

            response = await client.post(
                "/api/v1/admin/analytics/full-cleanup",
                headers={"Authorization": f"Bearer {token}"},
            )

            assert response.status_code == 403
        finally:
            await cleanup_admin_data(async_session)

    async def test_full_cleanup_dry_run(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test full-cleanup dry-run returns both job results."""
        await cleanup_admin_data(async_session)
        try:
            admin, token = await create_test_user(
                async_session, "admin_test@example.com", is_admin=True
            )

            mock_result = {
                "job": "analytics_cleanup",
                "dry_run": True,
                "timestamp": "2026-01-15T00:00:00+00:00",
                "anonymization": {
                    "job": "anonymize_old_events",
                    "dry_run": True,
                    "would_anonymize": 5,
                },
                "retention": {
                    "job": "delete_old_events",
                    "dry_run": True,
                    "would_delete": 3,
                },
            }

            with patch(
                "app.api.admin.analytics.analytics_cleanup_job", return_value=mock_result
            ):
                response = await client.post(
                    "/api/v1/admin/analytics/full-cleanup",
                    params={"dry_run": True},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["dry_run"] is True
            assert "anonymization" in data
            assert "retention" in data
        finally:
            await cleanup_admin_data(async_session)

    async def test_full_cleanup_runs_both_jobs(
        self, client: AsyncClient, async_session: AsyncSession
    ):
        """Test that full-cleanup runs both anonymization and retention."""
        await cleanup_admin_data(async_session)
        try:
            admin, token = await create_test_user(
                async_session, "admin_test@example.com", is_admin=True
            )

            mock_result = {
                "job": "analytics_cleanup",
                "dry_run": False,
                "timestamp": "2026-01-15T00:00:00+00:00",
                "anonymization": {
                    "job": "anonymize_old_events",
                    "dry_run": False,
                    "total_anonymized": 10,
                },
                "retention": {
                    "job": "delete_old_events",
                    "dry_run": False,
                    "total_deleted": 5,
                },
            }

            with patch(
                "app.api.admin.analytics.analytics_cleanup_job", return_value=mock_result
            ):
                response = await client.post(
                    "/api/v1/admin/analytics/full-cleanup",
                    params={"dry_run": False},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["dry_run"] is False
            assert "anonymization" in data
            assert "retention" in data
            assert data["anonymization"]["job"] == "anonymize_old_events"
            assert data["retention"]["job"] == "delete_old_events"
        finally:
            await cleanup_admin_data(async_session)
