"""Tests for feeding worker background jobs."""

import subprocess
import sys
import uuid
from datetime import date
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


# run_once tests


def test_dry_run_cannot_run_other_jobs():
    """Scoped recovery flags must be rejected before worker startup."""
    result = subprocess.run(
        [sys.executable, "-m", "app.workers.feeding_worker", "--run-once", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--job=check_subscriptions" in result.stderr


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--run-once", "--dry-run"], "--job=check_subscriptions"),
        (["--run-once", "--job=check_subscriptions", "--dry-run"], "requires --user-id"),
        (["--run-once", "--job=check_subscriptions", "--user-id", "not-a-uuid"], "invalid UUID"),
        (["--user-id", "11111111-1111-4111-8111-111111111111"], "require --run-once"),
    ],
)
def test_main_rejects_unsafe_subscription_flags_before_async_startup(argv, message, capsys):
    """Validation errors cannot initialize Redis or invoke a job."""
    from app.workers import feeding_worker

    with (
        patch.object(sys, "argv", ["feeding_worker", *argv]),
        patch.object(feeding_worker.asyncio, "run") as run,
        pytest.raises(SystemExit) as exit_info,
    ):
        feeding_worker.main()

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err
    run.assert_not_called()


def test_main_valid_subscription_path_initializes_then_closes_after_failure():
    """The real CLI coroutine chain forwards scoped flags and owns Redis cleanup."""
    from app.workers import feeding_worker

    user_id = uuid.uuid4()
    events: list[str] = []

    async def init_redis():
        events.append("init")

    async def close_redis():
        events.append("close")

    async def fail_job(*, dry_run, user_ids):
        assert events == ["init"]
        assert dry_run is True and user_ids == (user_id,)
        events.append("job")
        raise RuntimeError("subscription failure")

    with (
        patch.object(
            sys,
            "argv",
            ["feeding_worker", "--run-once", "--job=check_subscriptions", "--dry-run", "--user-id", str(user_id)],
        ),
        patch("app.redis.init_redis", side_effect=init_redis),
        patch("app.redis.close_redis", side_effect=close_redis),
        patch.object(feeding_worker, "check_expired_subscriptions_job", side_effect=fail_job),
    ):
        with pytest.raises(RuntimeError, match="subscription failure"):
            feeding_worker.main()

    assert events == ["init", "job", "close"]


@pytest.mark.asyncio(loop_scope="session")
async def test_valid_subscription_operation_uses_singleton_and_closes_after_failure():
    """A valid worker operation sees the initialized singleton and cleanup is unconditional."""
    from app.workers.feeding_worker import _run_with_redis

    client = MagicMock()

    async def failing_subscription_job():
        from app.redis import get_redis_client

        assert get_redis_client() is client
        raise RuntimeError("subscription failure")

    with (
        patch("app.redis.init_redis", new_callable=AsyncMock) as init_redis,
        patch("app.redis.close_redis", new_callable=AsyncMock) as close_redis,
        patch("app.redis.get_redis_client", return_value=client),
    ):
        with pytest.raises(RuntimeError, match="subscription failure"):
            await _run_with_redis(failing_subscription_job)

    init_redis.assert_awaited_once()
    close_redis.assert_awaited_once()


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_runs_all_jobs():
    """Test run_once executes all jobs when no specific job is specified."""
    from app.workers.feeding_worker import run_once

    with (
        patch("app.workers.feeding_worker.weekly_summary_job", new_callable=AsyncMock, return_value=0),
        patch("app.workers.feeding_worker.re_engagement_job", new_callable=AsyncMock, return_value=0),
        patch("app.workers.feeding_worker.check_expired_subscriptions_job", new_callable=AsyncMock, return_value=0),
        patch(
            "app.workers.feeding_worker.analytics_cleanup_job", new_callable=AsyncMock, return_value=0
        ) as mock_analytics,
        patch("app.workers.feeding_worker.image_cleanup_job", new_callable=AsyncMock, return_value={}),
        patch("app.workers.feeding_worker.s3_reconciliation_job", new_callable=AsyncMock, return_value={}),
    ):
        await run_once()
        mock_analytics.assert_called_once()


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_runs_specific_job():
    """Test run_once executes only specified job."""
    from app.workers.feeding_worker import run_once

    with (
        patch("app.workers.feeding_worker.weekly_summary_job", new_callable=AsyncMock, return_value=0) as mock_weekly,
        patch(
            "app.workers.feeding_worker.analytics_cleanup_job", new_callable=AsyncMock, return_value=0
        ) as mock_analytics,
    ):
        await run_once(job_name="analytics_cleanup")
        mock_analytics.assert_called_once()
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


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_reraises_subscription_error_for_all_jobs():
    """Subscription recovery failures make a one-shot process fail even in all-jobs mode."""
    from app.workers.feeding_worker import run_once

    with (
        patch("app.workers.feeding_worker.weekly_summary_job", new_callable=AsyncMock, return_value=0),
        patch("app.workers.feeding_worker.re_engagement_job", new_callable=AsyncMock, return_value=0),
        patch(
            "app.workers.feeding_worker.check_expired_subscriptions_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("incomplete reconciliation"),
        ),
    ):
        with pytest.raises(RuntimeError, match="incomplete"):
            await run_once()


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_reraises_subscription_error_for_explicit_job():
    """Explicit subscription recovery has the same nonzero one-shot behavior."""
    from app.workers.feeding_worker import run_once

    with patch(
        "app.workers.feeding_worker.check_expired_subscriptions_job",
        new_callable=AsyncMock,
        side_effect=RuntimeError("incomplete reconciliation"),
    ):
        with pytest.raises(RuntimeError, match="incomplete"):
            await run_once(job_name="check_subscriptions")


@pytest.mark.asyncio(loop_scope="session")
async def test_run_once_forwards_scoped_dry_run_user_ids_to_subscription_job():
    """The CLI dispatch target receives the exact scoped recovery arguments."""
    from app.workers.feeding_worker import run_once

    user_ids = (uuid.uuid4(), uuid.uuid4())
    with patch(
        "app.workers.feeding_worker.check_expired_subscriptions_job",
        new_callable=AsyncMock,
        return_value=2,
    ) as job:
        await run_once("check_subscriptions", dry_run=True, user_ids=user_ids)

    job.assert_awaited_once_with(dry_run=True, user_ids=user_ids)


@pytest.mark.asyncio(loop_scope="session")
async def test_standalone_worker_initializes_and_closes_redis_once():
    """The worker owns Redis because it does not run the FastAPI lifespan."""
    from app.workers.feeding_worker import _run_with_redis

    operation = AsyncMock()
    with (
        patch("app.redis.init_redis", new_callable=AsyncMock) as init_redis,
        patch("app.redis.close_redis", new_callable=AsyncMock) as close_redis,
    ):
        await _run_with_redis(operation)

    init_redis.assert_awaited_once()
    operation.assert_awaited_once()
    close_redis.assert_awaited_once()


# Scheduler startup tests


@pytest.mark.asyncio(loop_scope="session")
async def test_start_scheduler_registers_all_jobs():
    """Test that start_scheduler registers all expected jobs."""
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
            patch.object(feeding_worker, "AsyncScheduler", return_value=mock_scheduler),
            patch.object(feeding_worker, "SQLAlchemyDataStore"),
            patch.object(feeding_worker, "AsyncpgEventBroker"),
        ):
            await feeding_worker.start_scheduler()

            # Should have 7 jobs registered
            assert mock_scheduler.add_schedule.call_count == 7

            # Verify all expected jobs are registered
            job_ids = [call.kwargs["id"] for call in mock_scheduler.add_schedule.call_args_list]
            assert "image_cleanup" in job_ids
            assert "s3_reconciliation" in job_ids
            assert "backup_database" in job_ids
            assert "reset_stale_streaks" not in job_ids

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
            patch.object(feeding_worker, "AsyncScheduler", return_value=mock_scheduler),
            patch.object(feeding_worker, "SQLAlchemyDataStore"),
            patch.object(feeding_worker, "AsyncpgEventBroker"),
        ):
            await feeding_worker.start_scheduler()

            # Build a map of job_id → trigger for easy lookup
            schedule_calls = {call.kwargs["id"]: call.args[1] for call in mock_scheduler.add_schedule.call_args_list}

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
            patch.object(feeding_worker, "AsyncScheduler", return_value=mock_scheduler),
            patch.object(feeding_worker, "SQLAlchemyDataStore"),
            patch.object(feeding_worker, "AsyncpgEventBroker"),
        ):
            await feeding_worker.start_scheduler()

            mock_scheduler.start_in_background.assert_called_once()
    finally:
        feeding_worker._scheduler = original_scheduler
        feeding_worker._shutdown_event = original_shutdown_event
