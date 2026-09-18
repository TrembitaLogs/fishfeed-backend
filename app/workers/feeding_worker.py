"""Background worker for scheduled job management.

This module provides scheduled jobs for:
- Weekly summary notifications (Sunday at configured time)
- Re-engagement notifications for inactive users (daily)
- Subscription expiry checks
- Analytics cleanup
- Image cleanup: orphaned images garbage collection (daily at 04:00 UTC)
- S3 reconciliation: find unreferenced S3 objects (weekly Sunday at 05:00 UTC)

Schedule migration tracking:
    Increment SCHEDULE_VERSION when changing job schedules (triggers, timing).
    The version is stored in Redis so schedules are only re-registered on version bumps,
    avoiding unnecessary churn on every restart.

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
from uuid import UUID

import structlog
from apscheduler import AsyncScheduler, ConflictPolicy, JobOutcome, JobReleased
from apscheduler.datastores.sqlalchemy import SQLAlchemyDataStore
from apscheduler.eventbrokers.asyncpg import AsyncpgEventBroker
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.database import engine
from app.jobs.analytics_cleanup import analytics_cleanup_job
from app.jobs.backup_job import backup_database_job
from app.jobs.image_cleanup import image_cleanup_job, s3_reconciliation_job
from app.jobs.notification_jobs import re_engagement_job, weekly_summary_job
from app.jobs.subscription_jobs import check_expired_subscriptions_job
from app.services.purchase import RevenueCatAPIError

logger = structlog.get_logger(__name__)
settings = get_settings()

# Increment this when schedule definitions change (triggers, timing, new/removed jobs).
# On startup, schedules are only re-registered when this version differs from the
# stored value in Redis, preventing unnecessary ConflictPolicy.replace churn.
SCHEDULE_VERSION = 2
SCHEDULE_VERSION_KEY = "fishfeed:scheduler:schedule_version"

# Redis keys for job outcome tracking
JOB_FAILURE_KEY_PREFIX = "fishfeed:scheduler:job_failure:"
JOB_LAST_SUCCESS_KEY_PREFIX = "fishfeed:scheduler:job_last_success:"
JOB_FAILURE_TTL = 86400 * 7  # Keep failure records for 7 days

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
JOB_BACKUP_DATABASE = "backup_database"


def get_scheduler() -> AsyncScheduler | None:
    """Get the current scheduler instance.

    Returns:
        AsyncScheduler instance or None if not initialized.
    """
    return _scheduler


async def _handle_job_released(event: JobReleased) -> None:
    """Handle job completion events for alerting and tracking.

    Logs successes at info level, failures at error level with exception details.
    Tracks last success/failure timestamps in Redis for monitoring.
    """
    schedule_id = event.schedule_id or event.task_id or "unknown"
    outcome = event.outcome

    if outcome == JobOutcome.success:
        logger.info(
            "Job completed successfully",
            job_id=event.job_id,
            schedule_id=schedule_id,
            started_at=str(event.started_at),
        )
        try:
            from app.redis import get_redis_client

            redis = get_redis_client()
            key = f"{JOB_LAST_SUCCESS_KEY_PREFIX}{schedule_id}"
            await redis.set(key, event.timestamp.isoformat(), ex=JOB_FAILURE_TTL)
        except Exception:
            pass
    else:
        log_data = {
            "job_id": event.job_id,
            "schedule_id": schedule_id,
            "outcome": outcome.name,
            "started_at": str(event.started_at),
        }
        if event.exception_type:
            log_data["exception_type"] = event.exception_type
            log_data["exception_message"] = event.exception_message

        logger.error("Job failed", **log_data)

        # Track failure in Redis for monitoring/alerting
        try:
            from app.redis import get_redis_client

            redis = get_redis_client()
            failure_key = f"{JOB_FAILURE_KEY_PREFIX}{schedule_id}"
            await redis.lpush(  # type: ignore[misc]
                failure_key,
                f"{event.timestamp.isoformat()}|{outcome.name}|{event.exception_message or ''}",
            )
            await redis.ltrim(failure_key, 0, 49)  # type: ignore[misc]  # Keep last 50 failures
            await redis.expire(failure_key, JOB_FAILURE_TTL)
        except Exception:
            pass

        # Report to Sentry if available
        try:
            import sentry_sdk

            sentry_sdk.capture_message(
                f"Scheduler job failed: {schedule_id} ({outcome.name})",
                level="error",
                extras=log_data,
            )
        except Exception:
            pass


async def _needs_schedule_update() -> bool:
    """Check if schedule definitions need to be re-registered.

    Compares SCHEDULE_VERSION against the value stored in Redis.
    Returns True on first run or when version has been bumped.
    """
    try:
        from app.redis import get_redis_client

        redis = get_redis_client()
        stored = await redis.get(SCHEDULE_VERSION_KEY)
        if stored is not None and int(stored) == SCHEDULE_VERSION:
            return False
    except Exception:
        # Redis not available or not initialized — always update
        pass
    return True


async def _store_schedule_version() -> None:
    """Persist the current schedule version to Redis after successful registration."""
    try:
        from app.redis import get_redis_client

        redis = get_redis_client()
        await redis.set(SCHEDULE_VERSION_KEY, str(SCHEDULE_VERSION))
    except Exception as e:
        logger.warning("Failed to store schedule version in Redis", error=str(e))


async def _register_schedules(scheduler: AsyncScheduler) -> None:
    """Register all job schedules with the scheduler."""
    # Job 1: Weekly summary notifications (Sunday at configured time)
    await scheduler.add_schedule(
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
        schedule=(
            f"weekly on Sunday at {settings.NOTIFICATION_WEEKLY_SUMMARY_HOUR:02d}"
            f":{settings.NOTIFICATION_WEEKLY_SUMMARY_MINUTE:02d} UTC"
        ),
    )

    # Job 2: Re-engagement notifications (daily at configured time)
    await scheduler.add_schedule(
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
        schedule=(
            f"daily at {settings.NOTIFICATION_RE_ENGAGEMENT_HOUR:02d}"
            f":{settings.NOTIFICATION_RE_ENGAGEMENT_MINUTE:02d} UTC"
        ),
    )

    # APScheduler's max_running_jobs=1 default is sufficient for this single worker.
    # Job 3: Reconcile subscriptions (every N minutes)
    await scheduler.add_schedule(
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
    await scheduler.add_schedule(
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
    await scheduler.add_schedule(
        image_cleanup_job,
        CronTrigger(hour=4, minute=0, timezone=UTC),
        id=JOB_IMAGE_CLEANUP,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info("Added job", job_name=JOB_IMAGE_CLEANUP, schedule="daily at 04:00 UTC")

    # Job 6: S3 reconciliation - find unreferenced objects (weekly Sunday at 05:00 UTC)
    await scheduler.add_schedule(
        s3_reconciliation_job,
        CronTrigger(day_of_week="sun", hour=5, minute=0, timezone=UTC),
        id=JOB_S3_RECONCILIATION,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info("Added job", job_name=JOB_S3_RECONCILIATION, schedule="weekly on Sunday at 05:00 UTC")

    # Job 7: Database backup check (every N minutes; actual dump runs when the
    # per-DB interval has elapsed so admins can tune it from the UI).
    await scheduler.add_schedule(
        backup_database_job,
        IntervalTrigger(minutes=settings.BACKUP_CHECK_INTERVAL_MINUTES),
        id=JOB_BACKUP_DATABASE,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(
        "Added job",
        job_name=JOB_BACKUP_DATABASE,
        schedule=f"every {settings.BACKUP_CHECK_INTERVAL_MINUTES} minutes",
    )


async def start_scheduler() -> AsyncScheduler:
    """Initialize and start the scheduler with all jobs.

    Creates an AsyncScheduler with PostgreSQL datastore and asyncpg event broker.
    Schedules are only re-registered when SCHEDULE_VERSION changes, avoiding
    unnecessary churn on normal restarts.

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

    # Subscribe to job completion events for alerting
    _scheduler.subscribe(_handle_job_released, JobReleased)

    # Only re-register schedules when the version has changed
    if await _needs_schedule_update():
        logger.info("Schedule version changed, re-registering jobs", version=SCHEDULE_VERSION)
        await _register_schedules(_scheduler)
        await _store_schedule_version()
    else:
        logger.info("Schedule version unchanged, skipping re-registration", version=SCHEDULE_VERSION)

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


async def run_once(
    job_name: str | None = None,
    *,
    dry_run: bool = False,
    user_ids: tuple[UUID, ...] = (),
) -> None:
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
        "check_subscriptions": (
            "check_expired_subscriptions",
            lambda: check_expired_subscriptions_job(dry_run=dry_run, user_ids=user_ids),
        ),
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
            if name == "check_expired_subscriptions":
                metadata: dict[str, object] = {"error_type": type(e).__name__}
                if isinstance(e, RevenueCatAPIError):
                    metadata.update(upstream_status=e.upstream_status, retry_after_seconds=e.retry_after_seconds)
                logger.error("Subscription job failed", job_name=name, **metadata)
                raise
            logger.error("Job failed", job_name=name, error=str(e))


async def _run_with_redis(operation: Callable[[], Awaitable[None]]) -> None:
    """Give the standalone worker the same Redis lifecycle as the API lifespan."""
    from app.redis import close_redis, init_redis

    await init_redis()
    try:
        await operation()
    finally:
        await close_redis()


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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read only: reconcile explicit RevenueCat customer IDs without local writes",
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        action="append",
        default=[],
        help="RevenueCat customer UUID (repeatable; only with subscription recovery)",
    )

    args = parser.parse_args()

    if (args.dry_run or args.user_id) and not (args.run_once and args.job == "check_subscriptions"):
        parser.error("--dry-run/--user-id require --run-once --job=check_subscriptions")
    if args.dry_run and not args.user_id:
        parser.error("Live dry-run requires --user-id for confirmed RevenueCat customers")

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.run_once:
        try:
            asyncio.run(_run_with_redis(lambda: run_once(args.job, dry_run=args.dry_run, user_ids=tuple(args.user_id))))
        except Exception:
            if args.dry_run:
                raise SystemExit(1) from None
            raise
    else:
        asyncio.run(_run_with_redis(run_worker))


if __name__ == "__main__":
    main()
