"""Tests for analytics cleanup background jobs."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import AnalyticsEvent
from app.models.user import User


async def cleanup_analytics_data(session: AsyncSession) -> None:
    """Helper to cleanup analytics-related data."""
    await session.execute(text("DELETE FROM analytics_events"))
    await session.commit()


async def create_test_user(session: AsyncSession) -> User:
    """Helper to create a test user."""
    user = User(
        email=f"analytics-cleanup-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


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


# PII removal tests


def test_remove_pii_from_properties_empty():
    """Test PII removal from empty properties."""
    from app.jobs.analytics_cleanup import remove_pii_from_properties

    result = remove_pii_from_properties({})
    assert result == {}


def test_remove_pii_from_properties_none():
    """Test PII removal handles None."""
    from app.jobs.analytics_cleanup import remove_pii_from_properties

    result = remove_pii_from_properties(None)
    assert result == {}


def test_remove_pii_from_properties_redacts_email():
    """Test that email addresses are redacted."""
    from app.jobs.analytics_cleanup import remove_pii_from_properties

    props = {
        "message": "Contact me at john@example.com for details",
        "plain_key": "value",
    }
    result = remove_pii_from_properties(props)

    assert "[EMAIL_REDACTED]" in result["message"]
    assert "john@example.com" not in result["message"]
    assert result["plain_key"] == "value"


def test_remove_pii_from_properties_redacts_phone():
    """Test that phone numbers are redacted."""
    from app.jobs.analytics_cleanup import remove_pii_from_properties

    props = {
        "contact": "Call 555-123-4567 or +1 555 123 4567",
    }
    result = remove_pii_from_properties(props)

    assert "[PHONE_REDACTED]" in result["contact"]
    assert "555-123-4567" not in result["contact"]


def test_remove_pii_from_properties_redacts_pii_keys():
    """Test that known PII keys are fully redacted."""
    from app.jobs.analytics_cleanup import remove_pii_from_properties

    props = {
        "email": "user@example.com",
        "name": "John Doe",
        "phone": "+1-555-123-4567",
        "first_name": "John",
        "last_name": "Doe",
        "full_name": "John Doe",
        "address": "123 Main St",
        "safe_key": "safe_value",
    }
    result = remove_pii_from_properties(props)

    assert result["email"] == "[PII_REDACTED]"
    assert result["name"] == "[PII_REDACTED]"
    assert result["phone"] == "[PII_REDACTED]"
    assert result["first_name"] == "[PII_REDACTED]"
    assert result["last_name"] == "[PII_REDACTED]"
    assert result["full_name"] == "[PII_REDACTED]"
    assert result["address"] == "[PII_REDACTED]"
    assert result["safe_key"] == "safe_value"


def test_remove_pii_from_properties_handles_nested_dict():
    """Test that nested dictionaries are processed."""
    from app.jobs.analytics_cleanup import remove_pii_from_properties

    props = {
        "user": {
            "email": "nested@example.com",
            "id": "12345",
        },
    }
    result = remove_pii_from_properties(props)

    assert result["user"]["email"] == "[PII_REDACTED]"
    assert result["user"]["id"] == "12345"


def test_remove_pii_from_properties_handles_list():
    """Test that lists are processed."""
    from app.jobs.analytics_cleanup import remove_pii_from_properties

    props = {
        "items": [
            {"email": "first@example.com"},
            {"email": "second@example.com"},
        ],
    }
    result = remove_pii_from_properties(props)

    assert result["items"][0]["email"] == "[PII_REDACTED]"
    assert result["items"][1]["email"] == "[PII_REDACTED]"


def test_remove_pii_preserves_non_string_values():
    """Test that non-string values are preserved."""
    from app.jobs.analytics_cleanup import remove_pii_from_properties

    props = {
        "count": 42,
        "active": True,
        "ratio": 0.75,
        "empty": None,
    }
    result = remove_pii_from_properties(props)

    assert result["count"] == 42
    assert result["active"] is True
    assert result["ratio"] == 0.75
    assert result["empty"] is None


# Anonymization job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_anonymize_old_events_job_anonymizes_old_events(
    async_session: AsyncSession,
):
    """Test that old events are anonymized correctly."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import anonymize_old_events_job

        user = await create_test_user(async_session)

        # Create old event (35 days ago, should be anonymized)
        old_date = datetime.now(UTC) - timedelta(days=35)
        old_event = await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="old_event",
            properties={"email": "test@example.com"},
            ip_hash="original_hash",
            created_at=old_date,
        )

        # Create recent event (25 days ago, should NOT be anonymized)
        recent_date = datetime.now(UTC) - timedelta(days=25)
        recent_event = await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="recent_event",
            properties={"email": "test@example.com"},
            ip_hash="original_hash",
            created_at=recent_date,
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await anonymize_old_events_job(dry_run=False)

        # Refresh events
        await async_session.refresh(old_event)
        await async_session.refresh(recent_event)

        # Old event should be anonymized
        assert old_event.user_id is None
        assert old_event.ip_hash == "anonymized"
        assert old_event.anonymized_at is not None
        assert old_event.properties["email"] == "[PII_REDACTED]"

        # Recent event should NOT be anonymized
        assert recent_event.user_id == user.id
        assert recent_event.ip_hash == "original_hash"
        assert recent_event.anonymized_at is None

        assert result["total_anonymized"] == 1
    finally:
        await cleanup_analytics_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_anonymize_old_events_job_dry_run(async_session: AsyncSession):
    """Test that dry-run mode does not modify events."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import anonymize_old_events_job

        user = await create_test_user(async_session)

        # Create old event
        old_date = datetime.now(UTC) - timedelta(days=35)
        old_event = await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="old_event",
            properties={"email": "test@example.com"},
            ip_hash="original_hash",
            created_at=old_date,
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await anonymize_old_events_job(dry_run=True)

        # Refresh event
        await async_session.refresh(old_event)

        # Event should NOT be modified in dry-run mode
        assert old_event.user_id == user.id
        assert old_event.ip_hash == "original_hash"
        assert old_event.anonymized_at is None

        assert result["dry_run"] is True
        assert result["total_anonymized"] == 0
    finally:
        await cleanup_analytics_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_anonymize_old_events_job_skips_already_anonymized(
    async_session: AsyncSession,
):
    """Test that already anonymized events are skipped."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import anonymize_old_events_job

        # Create already anonymized event
        old_date = datetime.now(UTC) - timedelta(days=35)
        already_anonymized = await create_analytics_event(
            async_session,
            user_id=None,
            event_type="anonymized_event",
            properties={},
            ip_hash="anonymized",
            created_at=old_date,
            anonymized_at=datetime.now(UTC) - timedelta(days=5),
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await anonymize_old_events_job(dry_run=False)

        assert result["total_anonymized"] == 0
    finally:
        await cleanup_analytics_data(async_session)


# Retention (deletion) job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_old_events_job_deletes_old_events(async_session: AsyncSession):
    """Test that events older than retention period are deleted."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import delete_old_events_job

        user = await create_test_user(async_session)

        # Create very old event (100 days ago, should be deleted)
        very_old_date = datetime.now(UTC) - timedelta(days=100)
        very_old_event = await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="very_old_event",
            created_at=very_old_date,
        )
        very_old_id = very_old_event.id

        # Create event within retention (60 days ago, should NOT be deleted)
        recent_date = datetime.now(UTC) - timedelta(days=60)
        recent_event = await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="recent_event",
            created_at=recent_date,
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await delete_old_events_job(dry_run=False)

        # Check very old event is deleted
        stmt = select(AnalyticsEvent).where(AnalyticsEvent.id == very_old_id)
        query_result = await async_session.execute(stmt)
        assert query_result.scalar_one_or_none() is None

        # Check recent event is still there
        await async_session.refresh(recent_event)
        assert recent_event is not None
        assert recent_event.event_type == "recent_event"

        assert result["total_deleted"] == 1
    finally:
        await cleanup_analytics_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_old_events_job_dry_run(async_session: AsyncSession):
    """Test that dry-run mode does not delete events."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import delete_old_events_job

        user = await create_test_user(async_session)

        # Create very old event
        very_old_date = datetime.now(UTC) - timedelta(days=100)
        old_event = await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="very_old_event",
            created_at=very_old_date,
        )
        old_id = old_event.id

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await delete_old_events_job(dry_run=True)

        # Check event is NOT deleted
        stmt = select(AnalyticsEvent).where(AnalyticsEvent.id == old_id)
        query_result = await async_session.execute(stmt)
        assert query_result.scalar_one_or_none() is not None

        assert result["dry_run"] is True
        assert result["total_deleted"] == 0
        assert result["would_delete"] == 1
    finally:
        await cleanup_analytics_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_old_events_job_no_old_events(async_session: AsyncSession):
    """Test job when no events are old enough to delete."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import delete_old_events_job

        user = await create_test_user(async_session)

        # Create only recent events
        recent_date = datetime.now(UTC) - timedelta(days=10)
        await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="recent_event",
            created_at=recent_date,
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await delete_old_events_job(dry_run=False)

        assert result["total_deleted"] == 0
    finally:
        await cleanup_analytics_data(async_session)


# Combined analytics cleanup job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_analytics_cleanup_job_runs_both_jobs(async_session: AsyncSession):
    """Test that combined job runs both anonymization and retention."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import analytics_cleanup_job

        user = await create_test_user(async_session)

        # Create event for anonymization (35 days old)
        anonymize_date = datetime.now(UTC) - timedelta(days=35)
        await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="to_anonymize",
            created_at=anonymize_date,
        )

        # Create event for deletion (100 days old)
        delete_date = datetime.now(UTC) - timedelta(days=100)
        await create_analytics_event(
            async_session,
            user_id=user.id,
            event_type="to_delete",
            created_at=delete_date,
        )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await analytics_cleanup_job(dry_run=False)

        assert "anonymization" in result
        assert "retention" in result
        assert result["anonymization"]["total_anonymized"] >= 0
        assert result["retention"]["total_deleted"] >= 0
    finally:
        await cleanup_analytics_data(async_session)


# Batch processing tests


@pytest.mark.asyncio(loop_scope="session")
async def test_anonymize_old_events_processes_in_batches(async_session: AsyncSession):
    """Test that anonymization processes events in batches."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import anonymize_old_events_job

        user = await create_test_user(async_session)

        # Create multiple old events (more than batch size for testing)
        old_date = datetime.now(UTC) - timedelta(days=35)
        for i in range(50):
            await create_analytics_event(
                async_session,
                user_id=user.id,
                event_type=f"batch_event_{i}",
                created_at=old_date,
            )

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            # Use smaller batch size for testing
            with patch("app.jobs.analytics_cleanup.settings") as mock_settings:
                mock_settings.ANALYTICS_ANONYMIZE_AFTER_DAYS = 30
                mock_settings.ANALYTICS_CLEANUP_BATCH_SIZE = 10
                mock_settings.ANALYTICS_RETENTION_DAYS = 90

                result = await anonymize_old_events_job(dry_run=False)

        assert result["total_anonymized"] == 50
        # With batch size of 10 and 50 events, should process in multiple batches
        assert result["batches_processed"] >= 1
    finally:
        await cleanup_analytics_data(async_session)


# run_job helper tests


@pytest.mark.asyncio(loop_scope="session")
async def test_run_job_anonymize_only(async_session: AsyncSession):
    """Test run_job with anonymize-only option."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import run_job

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await run_job(job_name="anonymize", dry_run=True)

        assert result["job"] == "anonymize_old_events"
        assert "total_anonymized" in result or "would_anonymize" in result
    finally:
        await cleanup_analytics_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_run_job_retention_only(async_session: AsyncSession):
    """Test run_job with retention-only option."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import run_job

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await run_job(job_name="retention", dry_run=True)

        assert result["job"] == "delete_old_events"
        assert "total_deleted" in result or "would_delete" in result
    finally:
        await cleanup_analytics_data(async_session)


@pytest.mark.asyncio(loop_scope="session")
async def test_run_job_both(async_session: AsyncSession):
    """Test run_job with no specific job (runs both)."""
    await cleanup_analytics_data(async_session)
    try:
        from app.jobs.analytics_cleanup import run_job

        class MockSessionContext:
            async def __aenter__(self):
                return async_session

            async def __aexit__(self, *args):
                pass

        with patch(
            "app.jobs.analytics_cleanup.async_session_maker"
        ) as mock_session_maker:
            mock_session_maker.return_value = MockSessionContext()

            result = await run_job(job_name=None, dry_run=True)

        assert result["job"] == "analytics_cleanup"
        assert "anonymization" in result
        assert "retention" in result
    finally:
        await cleanup_analytics_data(async_session)
