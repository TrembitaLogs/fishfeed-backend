"""Tests for feeding worker background jobs."""

import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gamification import Streak
from app.models.user import User


async def create_test_user(
    session: AsyncSession,
    email: str | None = None,
) -> User:
    """Helper to create a test user."""
    user = User(
        email=email or f"test-{uuid.uuid4()}@example.com",
        password_hash="hashed_password",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def create_streak(
    session: AsyncSession,
    user_id: uuid.UUID,
    current_streak: int = 5,
    best_streak: int = 10,
    last_feed_date: date | None = None,
    freeze_available: int = 0,
) -> Streak:
    """Helper to create a streak record."""
    streak = Streak(
        user_id=user_id,
        current_streak=current_streak,
        best_streak=best_streak,
        last_feed_date=last_feed_date,
        freeze_available=freeze_available,
    )
    session.add(streak)
    await session.commit()
    await session.refresh(streak)
    return streak


# reset_stale_streaks_job tests


@pytest.mark.asyncio(loop_scope="session")
async def test_reset_stale_streaks_resets_old_streaks(async_session: AsyncSession):
    """Test that stale streaks are reset to 0 when no freeze is available."""
    from app.workers.feeding_worker import reset_stale_streaks_job

    user = await create_test_user(async_session)
    two_days_ago = date.today() - timedelta(days=2)
    streak = await create_streak(
        async_session,
        user.id,
        current_streak=7,
        last_feed_date=two_days_ago,
        freeze_available=0,
    )

    class MockSessionContext:
        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *args):
            pass

    with (
        patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker,
        patch(
            "app.workers.feeding_worker.NotificationService"
        ) as mock_notification_cls,
    ):
        mock_session_maker.return_value = MockSessionContext()
        mock_notification = MagicMock()
        mock_notification.send_push = AsyncMock(return_value=True)
        mock_notification_cls.return_value = mock_notification

        count = await reset_stale_streaks_job()

    assert count == 1

    await async_session.refresh(streak)
    assert streak.current_streak == 0
    # best_streak should remain unchanged
    assert streak.best_streak == 10


@pytest.mark.asyncio(loop_scope="session")
async def test_reset_stale_streaks_skips_users_with_freeze(async_session: AsyncSession):
    """Test that users with freeze days available are not reset."""
    from app.workers.feeding_worker import reset_stale_streaks_job

    user = await create_test_user(async_session)
    two_days_ago = date.today() - timedelta(days=2)
    streak = await create_streak(
        async_session,
        user.id,
        current_streak=5,
        last_feed_date=two_days_ago,
        freeze_available=1,
    )

    class MockSessionContext:
        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *args):
            pass

    with patch(
        "app.workers.feeding_worker.async_session_maker"
    ) as mock_session_maker:
        mock_session_maker.return_value = MockSessionContext()

        count = await reset_stale_streaks_job()

    assert count == 0

    await async_session.refresh(streak)
    assert streak.current_streak == 5


@pytest.mark.asyncio(loop_scope="session")
async def test_reset_stale_streaks_skips_recent_feeds(async_session: AsyncSession):
    """Test that users who fed yesterday are not reset."""
    from app.workers.feeding_worker import reset_stale_streaks_job

    user = await create_test_user(async_session)
    yesterday = date.today() - timedelta(days=1)
    streak = await create_streak(
        async_session,
        user.id,
        current_streak=3,
        last_feed_date=yesterday,
        freeze_available=0,
    )

    class MockSessionContext:
        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *args):
            pass

    with patch(
        "app.workers.feeding_worker.async_session_maker"
    ) as mock_session_maker:
        mock_session_maker.return_value = MockSessionContext()

        count = await reset_stale_streaks_job()

    assert count == 0

    await async_session.refresh(streak)
    assert streak.current_streak == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_reset_stale_streaks_skips_already_zero(async_session: AsyncSession):
    """Test that users with current_streak=0 are not processed."""
    from app.workers.feeding_worker import reset_stale_streaks_job

    user = await create_test_user(async_session)
    old_date = date.today() - timedelta(days=10)
    await create_streak(
        async_session,
        user.id,
        current_streak=0,
        last_feed_date=old_date,
        freeze_available=0,
    )

    class MockSessionContext:
        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *args):
            pass

    with patch(
        "app.workers.feeding_worker.async_session_maker"
    ) as mock_session_maker:
        mock_session_maker.return_value = MockSessionContext()

        count = await reset_stale_streaks_job()

    assert count == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_reset_stale_streaks_sends_notification(async_session: AsyncSession):
    """Test that push notification is sent when streak is reset."""
    from app.workers.feeding_worker import reset_stale_streaks_job

    user = await create_test_user(async_session)
    two_days_ago = date.today() - timedelta(days=2)
    await create_streak(
        async_session,
        user.id,
        current_streak=12,
        last_feed_date=two_days_ago,
        freeze_available=0,
    )

    class MockSessionContext:
        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *args):
            pass

    with (
        patch(
            "app.workers.feeding_worker.async_session_maker"
        ) as mock_session_maker,
        patch(
            "app.workers.feeding_worker.NotificationService"
        ) as mock_notification_cls,
    ):
        mock_session_maker.return_value = MockSessionContext()
        mock_notification = MagicMock()
        mock_notification.send_push = AsyncMock(return_value=True)
        mock_notification_cls.return_value = mock_notification

        await reset_stale_streaks_job()

    mock_notification.send_push.assert_called_once()
    call_kwargs = mock_notification.send_push.call_args[1]
    assert call_kwargs["user_id"] == user.id
    assert "12" in call_kwargs["body"]
    assert call_kwargs["notification_type"] == "streak_lost"


@pytest.mark.asyncio(loop_scope="session")
async def test_reset_stale_streaks_no_stale(async_session: AsyncSession):
    """Test that job returns 0 when no stale streaks exist."""
    from app.workers.feeding_worker import reset_stale_streaks_job

    class MockSessionContext:
        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *args):
            pass

    with patch(
        "app.workers.feeding_worker.async_session_maker"
    ) as mock_session_maker:
        mock_session_maker.return_value = MockSessionContext()

        count = await reset_stale_streaks_job()

    assert count == 0


# run_once tests


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_runs_all_jobs():
    """Test run_once executes all jobs when no specific job is specified."""
    from app.workers.feeding_worker import run_once

    with (
        patch("app.workers.feeding_worker.weekly_summary_job", new_callable=AsyncMock, return_value=0),
        patch("app.workers.feeding_worker.re_engagement_job", new_callable=AsyncMock, return_value=0),
        patch("app.workers.feeding_worker.check_expired_subscriptions_job", new_callable=AsyncMock, return_value=0),
        patch("app.workers.feeding_worker.analytics_cleanup_job", new_callable=AsyncMock, return_value=0),
        patch("app.workers.feeding_worker.reset_stale_streaks_job", new_callable=AsyncMock, return_value=0) as mock_reset,
        patch("app.workers.feeding_worker.image_cleanup_job", new_callable=AsyncMock, return_value={}),
        patch("app.workers.feeding_worker.s3_reconciliation_job", new_callable=AsyncMock, return_value={}),
    ):
        await run_once()
        mock_reset.assert_called_once()


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_runs_specific_job():
    """Test run_once executes only specified job."""
    from app.workers.feeding_worker import run_once

    with (
        patch("app.workers.feeding_worker.weekly_summary_job", new_callable=AsyncMock, return_value=0) as mock_weekly,
        patch("app.workers.feeding_worker.reset_stale_streaks_job", new_callable=AsyncMock, return_value=0) as mock_reset,
    ):
        await run_once(job_name="reset_stale_streaks")
        mock_reset.assert_called_once()
        mock_weekly.assert_not_called()


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_image_cleanup_job():
    """Test run_once executes only image_cleanup when specified."""
    from app.workers.feeding_worker import run_once

    with (
        patch(
            "app.workers.feeding_worker.image_cleanup_job",
            new_callable=AsyncMock,
            return_value={"job": "image_cleanup", "total_deleted_s3": 0},
        ) as mock_cleanup,
        patch(
            "app.workers.feeding_worker.s3_reconciliation_job",
            new_callable=AsyncMock,
            return_value={},
        ) as mock_reconciliation,
        patch(
            "app.workers.feeding_worker.weekly_summary_job",
            new_callable=AsyncMock,
            return_value=0,
        ) as mock_weekly,
    ):
        await run_once(job_name="image_cleanup")
        mock_cleanup.assert_called_once()
        mock_reconciliation.assert_not_called()
        mock_weekly.assert_not_called()


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_s3_reconciliation_job():
    """Test run_once executes only s3_reconciliation when specified."""
    from app.workers.feeding_worker import run_once

    with (
        patch(
            "app.workers.feeding_worker.s3_reconciliation_job",
            new_callable=AsyncMock,
            return_value={"job": "s3_reconciliation", "new_orphaned": 0},
        ) as mock_reconciliation,
        patch(
            "app.workers.feeding_worker.image_cleanup_job",
            new_callable=AsyncMock,
            return_value={},
        ) as mock_cleanup,
        patch(
            "app.workers.feeding_worker.weekly_summary_job",
            new_callable=AsyncMock,
            return_value=0,
        ) as mock_weekly,
    ):
        await run_once(job_name="s3_reconciliation")
        mock_reconciliation.assert_called_once()
        mock_cleanup.assert_not_called()
        mock_weekly.assert_not_called()


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_unknown_job():
    """Test run_once handles unknown job name."""
    from app.workers.feeding_worker import run_once

    # Should not raise, just log error
    await run_once(job_name="nonexistent_job")


# Scheduler startup tests


@pytest.mark.asyncio(loop_scope="session")
async def test_start_scheduler_registers_all_jobs():
    """Test that start_scheduler registers all expected jobs including reset_stale_streaks."""
    from app.workers import feeding_worker

    original_scheduler = feeding_worker._scheduler
    original_shutdown_event = feeding_worker._shutdown_event

    feeding_worker._scheduler = None
    feeding_worker._shutdown_event = None

    try:
        mock_scheduler = MagicMock()
        mock_scheduler.__aenter__ = AsyncMock(return_value=mock_scheduler)
        mock_scheduler.__aexit__ = AsyncMock(return_value=None)
        mock_scheduler.add_schedule = AsyncMock()
        mock_scheduler.start_in_background = AsyncMock()

        with (
            patch.object(
                feeding_worker, "AsyncScheduler", return_value=mock_scheduler
            ),
            patch.object(feeding_worker, "SQLAlchemyDataStore"),
            patch.object(feeding_worker, "AsyncpgEventBroker"),
        ):
            await feeding_worker.start_scheduler()

            # Should have 7 jobs registered
            assert mock_scheduler.add_schedule.call_count == 7

            # Verify all expected jobs are registered
            job_ids = [
                call.kwargs["id"]
                for call in mock_scheduler.add_schedule.call_args_list
            ]
            assert "reset_stale_streaks" in job_ids
            assert "image_cleanup" in job_ids
            assert "s3_reconciliation" in job_ids

            mock_scheduler.start_in_background.assert_called_once()
    finally:
        feeding_worker._scheduler = original_scheduler
        feeding_worker._shutdown_event = original_shutdown_event


@pytest.mark.asyncio(loop_scope="session")
async def test_start_scheduler_image_cleanup_cron_triggers():
    """Test that image_cleanup and s3_reconciliation have correct cron triggers."""
    from app.workers import feeding_worker

    original_scheduler = feeding_worker._scheduler
    original_shutdown_event = feeding_worker._shutdown_event

    feeding_worker._scheduler = None
    feeding_worker._shutdown_event = None

    try:
        mock_scheduler = MagicMock()
        mock_scheduler.__aenter__ = AsyncMock(return_value=mock_scheduler)
        mock_scheduler.__aexit__ = AsyncMock(return_value=None)
        mock_scheduler.add_schedule = AsyncMock()
        mock_scheduler.start_in_background = AsyncMock()

        with (
            patch.object(
                feeding_worker, "AsyncScheduler", return_value=mock_scheduler
            ),
            patch.object(feeding_worker, "SQLAlchemyDataStore"),
            patch.object(feeding_worker, "AsyncpgEventBroker"),
        ):
            await feeding_worker.start_scheduler()

            # Build a map of job_id → trigger for easy lookup
            schedule_calls = {
                call.kwargs["id"]: call.args[1]
                for call in mock_scheduler.add_schedule.call_args_list
            }

            # image_cleanup: daily at 04:00 UTC
            cleanup_trigger = schedule_calls["image_cleanup"]
            assert cleanup_trigger.hour == 4
            assert cleanup_trigger.minute == 0

            # s3_reconciliation: weekly Sunday at 05:00 UTC
            recon_trigger = schedule_calls["s3_reconciliation"]
            assert recon_trigger.day_of_week == "sun"
            assert recon_trigger.hour == 5
            assert recon_trigger.minute == 0
    finally:
        feeding_worker._scheduler = original_scheduler
        feeding_worker._shutdown_event = original_shutdown_event


@pytest.mark.asyncio(loop_scope="session")
async def test_start_scheduler_calls_start_in_background():
    """Test that start_scheduler() calls start_in_background() to process jobs."""
    from app.workers import feeding_worker

    original_scheduler = feeding_worker._scheduler
    original_shutdown_event = feeding_worker._shutdown_event

    feeding_worker._scheduler = None
    feeding_worker._shutdown_event = None

    try:
        mock_scheduler = MagicMock()
        mock_scheduler.__aenter__ = AsyncMock(return_value=mock_scheduler)
        mock_scheduler.__aexit__ = AsyncMock(return_value=None)
        mock_scheduler.add_schedule = AsyncMock()
        mock_scheduler.start_in_background = AsyncMock()

        with (
            patch.object(
                feeding_worker, "AsyncScheduler", return_value=mock_scheduler
            ),
            patch.object(feeding_worker, "SQLAlchemyDataStore"),
            patch.object(feeding_worker, "AsyncpgEventBroker"),
        ):
            await feeding_worker.start_scheduler()

            mock_scheduler.start_in_background.assert_called_once()
    finally:
        feeding_worker._scheduler = original_scheduler
        feeding_worker._shutdown_event = original_shutdown_event
