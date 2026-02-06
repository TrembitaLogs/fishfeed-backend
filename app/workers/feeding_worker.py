"""Background worker for scheduled job management.

This module provides scheduled jobs for:
- Weekly summary notifications (Sunday at configured time)
- Re-engagement notifications for inactive users (daily)
- Subscription expiry checks
- Analytics cleanup
- Stale streak reset (daily at 02:00 UTC)

Usage:
    # Run as standalone worker
    python -m app.workers.feeding_worker

    # Run once for testing
    python -m app.workers.feeding_worker --run-once

    # Run specific job
    python -m app.workers.feeding_worker --run-once --job=weekly_summary
"""

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, date, timedelta
from typing import Any

from apscheduler import AsyncScheduler, ConflictPolicy
from apscheduler.datastores.sqlalchemy import SQLAlchemyDataStore
from apscheduler.eventbrokers.asyncpg import AsyncpgEventBroker
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session_maker, engine
from app.jobs.analytics_cleanup import analytics_cleanup_job
from app.jobs.notification_jobs import re_engagement_job, weekly_summary_job
from app.jobs.subscription_jobs import check_expired_subscriptions_job
from app.models.gamification import Streak
from app.services.notification import NotificationService

logger = logging.getLogger(__name__)
settings = get_settings()

# Global scheduler instance
_scheduler: AsyncScheduler | None = None
_shutdown_event: asyncio.Event | None = None

# Job IDs
JOB_WEEKLY_SUMMARY = "weekly_summary"
JOB_RE_ENGAGEMENT = "re_engagement"
JOB_CHECK_EXPIRED_SUBSCRIPTIONS = "check_expired_subscriptions"
JOB_ANALYTICS_CLEANUP = "analytics_cleanup"
JOB_RESET_STALE_STREAKS = "reset_stale_streaks"


async def reset_stale_streaks_job() -> int:
    """Reset streaks for users who missed feeding and have no freeze days.

    Finds users whose last_feed_date is older than yesterday, have a positive
    current_streak, and no freeze days available. Resets their streak to 0
    and sends a push notification about the lost streak.

    Returns:
        Number of streaks reset.
    """
    logger.info("Starting reset_stale_streaks_job")

    yesterday = date.today() - timedelta(days=1)

    async with async_session_maker() as db:
        stmt = select(Streak).where(
            Streak.last_feed_date < yesterday,
            Streak.current_streak > 0,
            Streak.freeze_available <= 0,
        )
        result = await db.execute(stmt)
        stale_streaks = list(result.scalars().all())

        if not stale_streaks:
            logger.info("No stale streaks found")
            return 0

        affected: list[tuple] = []
        for streak in stale_streaks:
            lost_streak = streak.current_streak
            affected.append((streak.user_id, lost_streak))
            streak.current_streak = 0

        await db.commit()

        notification_service = NotificationService(db)
        for user_id, lost_streak in affected:
            try:
                await notification_service.send_push(
                    user_id=user_id,
                    title="Streak lost",
                    body=(
                        f"Your {lost_streak}-day streak has been reset. "
                        "Feed your fish to start again!"
                    ),
                    data={
                        "type": "streak_lost",
                        "lost_streak": str(lost_streak),
                    },
                    notification_type="streak_lost",
                )
            except Exception as e:
                logger.error(
                    f"Failed to send streak lost notification to user {user_id}: {e}"
                )

        logger.info(f"Reset {len(affected)} stale streaks")
        return len(affected)


def get_scheduler() -> AsyncScheduler | None:
    """Get the current scheduler instance.

    Returns:
        AsyncScheduler instance or None if not initialized.
    """
    return _scheduler


async def start_scheduler() -> AsyncScheduler:
    """Initialize and start the scheduler with all jobs.

    Creates an AsyncScheduler with PostgreSQL datastore and asyncpg event broker,
    adds all scheduled jobs, and starts the scheduler.

    Returns:
        Started AsyncScheduler instance.
    """
    global _scheduler, _shutdown_event

    if _scheduler is not None:
        logger.warning("Scheduler already running")
        return _scheduler

    logger.info("Initializing scheduler...")

    # Create data store and event broker
    data_store = SQLAlchemyDataStore(engine)
    event_broker = AsyncpgEventBroker.from_async_sqla_engine(engine)

    # Create scheduler
    _scheduler = AsyncScheduler(data_store, event_broker)
    _shutdown_event = asyncio.Event()

    # Start scheduler context
    await _scheduler.__aenter__()

    # Job 1: Weekly summary notifications (Sunday at configured time)
    await _scheduler.add_schedule(
        weekly_summary_job,
        CronTrigger(
            day_of_week="sun",
            hour=settings.NOTIFICATION_WEEKLY_SUMMARY_HOUR,
            minute=settings.NOTIFICATION_WEEKLY_SUMMARY_MINUTE,
            timezone=UTC,
        ),
        id=JOB_WEEKLY_SUMMARY,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(
        f"Added job '{JOB_WEEKLY_SUMMARY}' "
        f"(weekly on Sunday at {settings.NOTIFICATION_WEEKLY_SUMMARY_HOUR:02d}:"
        f"{settings.NOTIFICATION_WEEKLY_SUMMARY_MINUTE:02d} UTC)"
    )

    # Job 2: Re-engagement notifications (daily at configured time)
    await _scheduler.add_schedule(
        re_engagement_job,
        CronTrigger(
            hour=settings.NOTIFICATION_RE_ENGAGEMENT_HOUR,
            minute=settings.NOTIFICATION_RE_ENGAGEMENT_MINUTE,
            timezone=UTC,
        ),
        id=JOB_RE_ENGAGEMENT,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(
        f"Added job '{JOB_RE_ENGAGEMENT}' "
        f"(daily at {settings.NOTIFICATION_RE_ENGAGEMENT_HOUR:02d}:"
        f"{settings.NOTIFICATION_RE_ENGAGEMENT_MINUTE:02d} UTC)"
    )

    # Job 3: Check expired subscriptions (every N minutes)
    await _scheduler.add_schedule(
        check_expired_subscriptions_job,
        IntervalTrigger(minutes=settings.SUBSCRIPTION_CHECK_INTERVAL_MINUTES),
        id=JOB_CHECK_EXPIRED_SUBSCRIPTIONS,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(
        f"Added job '{JOB_CHECK_EXPIRED_SUBSCRIPTIONS}' "
        f"(every {settings.SUBSCRIPTION_CHECK_INTERVAL_MINUTES} minutes)"
    )

    # Job 4: Analytics cleanup - anonymization and retention (daily at configured time)
    await _scheduler.add_schedule(
        analytics_cleanup_job,
        CronTrigger(
            hour=settings.ANALYTICS_CLEANUP_HOUR,
            minute=settings.ANALYTICS_CLEANUP_MINUTE,
            timezone=UTC,
        ),
        id=JOB_ANALYTICS_CLEANUP,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(
        f"Added job '{JOB_ANALYTICS_CLEANUP}' "
        f"(daily at {settings.ANALYTICS_CLEANUP_HOUR:02d}:{settings.ANALYTICS_CLEANUP_MINUTE:02d} UTC)"
    )

    # Job 5: Reset stale streaks (daily at 02:00 UTC)
    await _scheduler.add_schedule(
        reset_stale_streaks_job,
        CronTrigger(hour=2, minute=0, timezone=UTC),
        id=JOB_RESET_STALE_STREAKS,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(f"Added job '{JOB_RESET_STALE_STREAKS}' (daily at 02:00 UTC)")

    # Start processing schedules in background
    await _scheduler.start_in_background()

    logger.info("Scheduler started successfully")
    return _scheduler


async def stop_scheduler() -> None:
    """Stop the scheduler gracefully.

    Waits for running jobs to complete before shutting down.
    """
    global _scheduler, _shutdown_event

    if _scheduler is None:
        logger.warning("Scheduler not running")
        return

    logger.info("Stopping scheduler...")

    if _shutdown_event:
        _shutdown_event.set()

    await _scheduler.__aexit__(None, None, None)
    _scheduler = None
    _shutdown_event = None

    logger.info("Scheduler stopped")


async def run_once(job_name: str | None = None) -> None:
    """Run jobs once for testing/debugging.

    Args:
        job_name: Optional specific job to run. If None, runs all jobs.
                  Valid values: 'weekly_summary', 're_engagement',
                               'check_subscriptions', 'analytics_cleanup'
    """
    logger.info(f"Running jobs once (job_name={job_name})")

    JobFunc = Callable[[], Awaitable[Any]]
    jobs: dict[str, tuple[str, JobFunc]] = {
        "weekly_summary": ("weekly_summary", weekly_summary_job),
        "re_engagement": ("re_engagement", re_engagement_job),
        "check_subscriptions": ("check_expired_subscriptions", check_expired_subscriptions_job),
        "analytics_cleanup": ("analytics_cleanup", analytics_cleanup_job),
        "reset_stale_streaks": ("reset_stale_streaks", reset_stale_streaks_job),
    }

    jobs_to_run: list[tuple[str, JobFunc]]
    if job_name:
        if job_name not in jobs:
            logger.error(f"Unknown job: {job_name}. Valid: {list(jobs.keys())}")
            return
        jobs_to_run = [jobs[job_name]]
    else:
        jobs_to_run = list(jobs.values())

    for name, job_func in jobs_to_run:
        logger.info(f"Running job: {name}")
        try:
            result = await job_func()
            logger.info(f"Job {name} completed: {result}")
        except Exception as e:
            logger.error(f"Job {name} failed: {e}")


async def run_worker() -> None:
    """Run the worker as a standalone process.

    Starts the scheduler and runs until SIGTERM/SIGINT is received.
    """
    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def handle_signal(sig: signal.Signals) -> None:
        logger.info(f"Received signal {sig.name}, shutting down...")
        if _shutdown_event:
            _shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))  # type: ignore[misc]

    logger.info("Starting FishFeed worker...")

    await start_scheduler()

    if _shutdown_event:
        await _shutdown_event.wait()

    await stop_scheduler()
    logger.info("Worker stopped")


def main() -> None:
    """CLI entry point for the worker."""
    parser = argparse.ArgumentParser(description="FishFeed background worker")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run jobs once and exit (for testing/debugging)",
    )
    parser.add_argument(
        "--job",
        type=str,
        choices=[
            "weekly_summary",
            "re_engagement",
            "check_subscriptions",
            "analytics_cleanup",
            "reset_stale_streaks",
        ],
        help="Specific job to run (only with --run-once)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.run_once:
        asyncio.run(run_once(args.job))
    else:
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
