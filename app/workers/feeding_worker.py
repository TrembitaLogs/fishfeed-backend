"""Background worker for scheduled job management.

This module provides scheduled jobs for:
- Weekly summary notifications (Sunday at configured time)
- Re-engagement notifications for inactive users (daily)
- Subscription expiry checks
- Analytics cleanup
- Image cleanup: orphaned images garbage collection (daily at 04:00 UTC)
- S3 reconciliation: find unreferenced S3 objects (weekly Sunday at 05:00 UTC)

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
from datetime import UTC
from typing import Any

import structlog
from apscheduler import AsyncScheduler, ConflictPolicy
from apscheduler.datastores.sqlalchemy import SQLAlchemyDataStore
from apscheduler.eventbrokers.asyncpg import AsyncpgEventBroker
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.database import engine
from app.jobs.analytics_cleanup import analytics_cleanup_job
from app.jobs.image_cleanup import image_cleanup_job, s3_reconciliation_job
from app.jobs.notification_jobs import re_engagement_job, weekly_summary_job
from app.jobs.subscription_jobs import check_expired_subscriptions_job

logger = structlog.get_logger(__name__)
settings = get_settings()

# Global scheduler instance
_scheduler: AsyncScheduler | None = None
_shutdown_event: asyncio.Event | None = None

# Job IDs
JOB_WEEKLY_SUMMARY = "weekly_summary"
JOB_RE_ENGAGEMENT = "re_engagement"
JOB_CHECK_EXPIRED_SUBSCRIPTIONS = "check_expired_subscriptions"
JOB_ANALYTICS_CLEANUP = "analytics_cleanup"
JOB_IMAGE_CLEANUP = "image_cleanup"
JOB_S3_RECONCILIATION = "s3_reconciliation"


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
        "Added job",
        job_name=JOB_WEEKLY_SUMMARY,
        schedule=f"weekly on Sunday at {settings.NOTIFICATION_WEEKLY_SUMMARY_HOUR:02d}:{settings.NOTIFICATION_WEEKLY_SUMMARY_MINUTE:02d} UTC",
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
        "Added job",
        job_name=JOB_RE_ENGAGEMENT,
        schedule=f"daily at {settings.NOTIFICATION_RE_ENGAGEMENT_HOUR:02d}:{settings.NOTIFICATION_RE_ENGAGEMENT_MINUTE:02d} UTC",
    )

    # Job 3: Check expired subscriptions (every N minutes)
    await _scheduler.add_schedule(
        check_expired_subscriptions_job,
        IntervalTrigger(minutes=settings.SUBSCRIPTION_CHECK_INTERVAL_MINUTES),
        id=JOB_CHECK_EXPIRED_SUBSCRIPTIONS,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(
        "Added job",
        job_name=JOB_CHECK_EXPIRED_SUBSCRIPTIONS,
        schedule=f"every {settings.SUBSCRIPTION_CHECK_INTERVAL_MINUTES} minutes",
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
        "Added job",
        job_name=JOB_ANALYTICS_CLEANUP,
        schedule=f"daily at {settings.ANALYTICS_CLEANUP_HOUR:02d}:{settings.ANALYTICS_CLEANUP_MINUTE:02d} UTC",
    )

    # Job 5: Image cleanup - delete orphaned images older than 7 days (daily at 04:00 UTC)
    await _scheduler.add_schedule(
        image_cleanup_job,
        CronTrigger(hour=4, minute=0, timezone=UTC),
        id=JOB_IMAGE_CLEANUP,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info("Added job", job_name=JOB_IMAGE_CLEANUP, schedule="daily at 04:00 UTC")

    # Job 6: S3 reconciliation - find unreferenced objects (weekly Sunday at 05:00 UTC)
    await _scheduler.add_schedule(
        s3_reconciliation_job,
        CronTrigger(day_of_week="sun", hour=5, minute=0, timezone=UTC),
        id=JOB_S3_RECONCILIATION,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info("Added job", job_name=JOB_S3_RECONCILIATION, schedule="weekly on Sunday at 05:00 UTC")

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
                               'check_subscriptions', 'analytics_cleanup',
                               'image_cleanup', 's3_reconciliation'
    """
    logger.info("Running jobs once", job_name=job_name)

    JobFunc = Callable[[], Awaitable[Any]]
    jobs: dict[str, tuple[str, JobFunc]] = {
        "weekly_summary": ("weekly_summary", weekly_summary_job),
        "re_engagement": ("re_engagement", re_engagement_job),
        "check_subscriptions": ("check_expired_subscriptions", check_expired_subscriptions_job),
        "analytics_cleanup": ("analytics_cleanup", analytics_cleanup_job),
        "image_cleanup": ("image_cleanup", image_cleanup_job),
        "s3_reconciliation": ("s3_reconciliation", s3_reconciliation_job),
    }

    jobs_to_run: list[tuple[str, JobFunc]]
    if job_name:
        if job_name not in jobs:
            logger.error("Unknown job", job_name=job_name, valid_jobs=list(jobs.keys()))
            return
        jobs_to_run = [jobs[job_name]]
    else:
        jobs_to_run = list(jobs.values())

    for name, job_func in jobs_to_run:
        logger.info("Running job", job_name=name)
        try:
            result = await job_func()
            logger.info("Job completed", job_name=name, result=result)
        except Exception as e:
            logger.error("Job failed", job_name=name, error=str(e))


async def run_worker() -> None:
    """Run the worker as a standalone process.

    Starts the scheduler and runs until SIGTERM/SIGINT is received.
    """
    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def handle_signal(sig: signal.Signals) -> None:
        logger.info("Received signal, shutting down", signal_name=sig.name)
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
            "image_cleanup",
            "s3_reconciliation",
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
