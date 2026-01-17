"""Background worker for feeding event management.

This module provides scheduled jobs for:
- Creating tomorrow's feeding events (daily at 23:00 UTC)
- Marking overdue events as missed (every 15 minutes)
- Cleaning up old events (weekly)

Usage:
    # Run as standalone worker
    python -m app.workers.feeding_worker

    # Run once for testing
    python -m app.workers.feeding_worker --run-once

    # Run specific job
    python -m app.workers.feeding_worker --run-once --job=create_events
"""

import argparse
import asyncio
import logging
import signal
from datetime import UTC, date, datetime, timedelta

from apscheduler import AsyncScheduler, ConflictPolicy
from apscheduler.datastores.sqlalchemy import SQLAlchemyDataStore
from apscheduler.eventbrokers.asyncpg import AsyncpgEventBroker
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_maker, engine
from app.jobs.analytics_cleanup import analytics_cleanup_job
from app.jobs.notification_jobs import re_engagement_job, weekly_summary_job
from app.jobs.subscription_jobs import check_expired_subscriptions_job
from app.models.feeding import FeedingEvent
from app.services.feeding import create_daily_events

logger = logging.getLogger(__name__)
settings = get_settings()

# Global scheduler instance
_scheduler: AsyncScheduler | None = None
_shutdown_event: asyncio.Event | None = None

# Job IDs
JOB_CREATE_TOMORROW_EVENTS = "create_tomorrow_events"
JOB_MARK_OVERDUE_MISSED = "mark_overdue_as_missed"
JOB_CLEANUP_OLD_EVENTS = "cleanup_old_events"
JOB_WEEKLY_SUMMARY = "weekly_summary"
JOB_RE_ENGAGEMENT = "re_engagement"
JOB_CHECK_EXPIRED_SUBSCRIPTIONS = "check_expired_subscriptions"
JOB_ANALYTICS_CLEANUP = "analytics_cleanup"


async def create_tomorrow_events_job() -> int:
    """Create feeding events for tomorrow for all active schedules.

    This job runs daily at 23:00 UTC to pre-create next day's events.
    Implements retry logic with max 3 attempts.

    Returns:
        Number of events created.
    """
    tomorrow = date.today() + timedelta(days=1)
    logger.info(f"Starting create_tomorrow_events job for {tomorrow}")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            async with async_session_maker() as db:
                created_count = await create_daily_events(db, tomorrow)
                logger.info(
                    f"Created {created_count} feeding events for {tomorrow} "
                    f"(attempt {attempt}/{max_attempts})"
                )
                return created_count
        except Exception as e:
            logger.error(
                f"Failed to create events for {tomorrow} "
                f"(attempt {attempt}/{max_attempts}): {e}"
            )
            if attempt == max_attempts:
                logger.error(f"All {max_attempts} attempts failed for create_tomorrow_events")
                raise
            await asyncio.sleep(5 * attempt)  # Exponential backoff

    return 0


async def mark_overdue_as_missed_job() -> int:
    """Mark overdue pending events as missed.

    Finds all pending events where scheduled_at < now() - threshold hours
    and updates their status to 'missed'.

    Returns:
        Number of events marked as missed.
    """
    threshold_hours = settings.WORKER_OVERDUE_THRESHOLD_HOURS
    cutoff_time = datetime.now(UTC) - timedelta(hours=threshold_hours)

    logger.info(f"Checking for overdue events (scheduled before {cutoff_time})")

    async with async_session_maker() as db:
        # Find overdue pending events
        stmt = (
            select(FeedingEvent)
            .where(FeedingEvent.status == "pending")
            .where(FeedingEvent.scheduled_at < cutoff_time)
        )
        result = await db.execute(stmt)
        overdue_events = list(result.scalars().all())

        if not overdue_events:
            logger.debug("No overdue events found")
            return 0

        # Update all overdue events to missed
        event_ids = [event.id for event in overdue_events]
        update_stmt = (
            update(FeedingEvent)
            .where(FeedingEvent.id.in_(event_ids))
            .values(status="missed")
        )
        await db.execute(update_stmt)
        await db.commit()

        logger.info(f"Marked {len(overdue_events)} events as missed")

        # Check for streak breaks (all events of a day missed)
        await _check_streak_breaks(db, overdue_events)

        return len(overdue_events)


async def _check_streak_breaks(
    db: AsyncSession,
    missed_events: list[FeedingEvent],
) -> None:
    """Check if all events for any aquarium on a date are missed.

    If all feeding events for an aquarium on a specific date are missed,
    this triggers a streak break notification (to be implemented in Task 10).

    Args:
        db: Database session.
        missed_events: List of events just marked as missed.
    """
    # Group missed events by aquarium and date
    aquarium_dates: dict[tuple, list[FeedingEvent]] = {}
    for event in missed_events:
        key = (event.aquarium_id, event.scheduled_at.date())
        if key not in aquarium_dates:
            aquarium_dates[key] = []
        aquarium_dates[key].append(event)

    for (aquarium_id, event_date), _events in aquarium_dates.items():
        # Check if all events for this aquarium on this date are missed
        date_start = datetime.combine(
            event_date,
            datetime.min.time(),
            tzinfo=UTC,
        )
        date_end = datetime.combine(
            event_date,
            datetime.max.time(),
            tzinfo=UTC,
        )

        all_events_stmt = (
            select(FeedingEvent)
            .where(FeedingEvent.aquarium_id == aquarium_id)
            .where(FeedingEvent.scheduled_at >= date_start)
            .where(FeedingEvent.scheduled_at <= date_end)
        )
        result = await db.execute(all_events_stmt)
        all_day_events = list(result.scalars().all())

        # Check if all events are either missed or still pending (will be missed)
        all_missed = all(event.status == "missed" for event in all_day_events)

        if all_missed and all_day_events:
            logger.warning(
                f"Streak break detected for aquarium {aquarium_id} on {event_date}: "
                f"all {len(all_day_events)} feeding events were missed"
            )
            # TODO: Trigger streak break notification (Task 10)


async def cleanup_old_events_job() -> int:
    """Clean up feeding events older than retention period.

    Deletes events older than WORKER_CLEANUP_RETENTION_DAYS.
    Statistics are logged before deletion for archival purposes.

    Returns:
        Number of events deleted.
    """
    retention_days = settings.WORKER_CLEANUP_RETENTION_DAYS
    cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)

    logger.info(f"Cleaning up events older than {cutoff_date.date()}")

    async with async_session_maker() as db:
        # Get statistics before deletion for archival logging
        stats_stmt = (
            select(
                FeedingEvent.aquarium_id,
                FeedingEvent.status,
            )
            .where(FeedingEvent.scheduled_at < cutoff_date)
        )
        result = await db.execute(stats_stmt)
        old_events = result.all()

        if not old_events:
            logger.debug("No old events to clean up")
            return 0

        # Aggregate statistics
        stats: dict[str, int] = {"completed": 0, "missed": 0, "pending": 0}
        aquariums: set = set()
        for event in old_events:
            aquariums.add(event.aquarium_id)
            stats[event.status] = stats.get(event.status, 0) + 1

        total_count = len(old_events)

        # Log statistics for archival
        logger.info(
            f"Archiving statistics before cleanup: "
            f"total={total_count}, completed={stats.get('completed', 0)}, "
            f"missed={stats.get('missed', 0)}, pending={stats.get('pending', 0)}, "
            f"aquariums_affected={len(aquariums)}"
        )

        # Delete old events
        delete_stmt = delete(FeedingEvent).where(FeedingEvent.scheduled_at < cutoff_date)
        await db.execute(delete_stmt)
        await db.commit()

        logger.info(f"Deleted {total_count} old feeding events")
        return total_count


def get_scheduler() -> AsyncScheduler | None:
    """Get the current scheduler instance.

    Returns:
        AsyncScheduler instance or None if not initialized.
    """
    return _scheduler


async def start_scheduler() -> AsyncScheduler:
    """Initialize and start the scheduler with all jobs.

    Creates an AsyncScheduler with PostgreSQL datastore and Redis event broker,
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

    # Add scheduled jobs
    # Job 1: Create tomorrow's events daily at configured time (default 23:00 UTC)
    await _scheduler.add_schedule(
        create_tomorrow_events_job,
        CronTrigger(
            hour=settings.WORKER_CREATE_EVENTS_HOUR,
            minute=settings.WORKER_CREATE_EVENTS_MINUTE,
            timezone=UTC,
        ),
        id=JOB_CREATE_TOMORROW_EVENTS,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(
        f"Added job '{JOB_CREATE_TOMORROW_EVENTS}' "
        f"(daily at {settings.WORKER_CREATE_EVENTS_HOUR:02d}:{settings.WORKER_CREATE_EVENTS_MINUTE:02d} UTC)"
    )

    # Job 2: Mark overdue events as missed every N minutes
    await _scheduler.add_schedule(
        mark_overdue_as_missed_job,
        IntervalTrigger(minutes=settings.WORKER_OVERDUE_CHECK_MINUTES),
        id=JOB_MARK_OVERDUE_MISSED,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(
        f"Added job '{JOB_MARK_OVERDUE_MISSED}' "
        f"(every {settings.WORKER_OVERDUE_CHECK_MINUTES} minutes)"
    )

    # Job 3: Cleanup old events weekly (Sunday at 03:00 UTC)
    await _scheduler.add_schedule(
        cleanup_old_events_job,
        CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=UTC),
        id=JOB_CLEANUP_OLD_EVENTS,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info(f"Added job '{JOB_CLEANUP_OLD_EVENTS}' (weekly on Sunday at 03:00 UTC)")

    # Job 4: Weekly summary notifications (Sunday at configured time)
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

    # Job 5: Re-engagement notifications (daily at configured time)
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

    # Job 6: Check expired subscriptions (every N minutes)
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

    # Job 7: Analytics cleanup - anonymization and retention (daily at configured time)
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
                  Valid values: 'create_events', 'mark_missed', 'cleanup',
                               'weekly_summary', 're_engagement'
    """
    logger.info(f"Running jobs once (job_name={job_name})")

    jobs = {
        "create_events": ("create_tomorrow_events", create_tomorrow_events_job),
        "mark_missed": ("mark_overdue_as_missed", mark_overdue_as_missed_job),
        "cleanup": ("cleanup_old_events", cleanup_old_events_job),
        "weekly_summary": ("weekly_summary", weekly_summary_job),
        "re_engagement": ("re_engagement", re_engagement_job),
        "check_subscriptions": ("check_expired_subscriptions", check_expired_subscriptions_job),
        "analytics_cleanup": ("analytics_cleanup", analytics_cleanup_job),
    }

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
            "create_events",
            "mark_missed",
            "cleanup",
            "weekly_summary",
            "re_engagement",
            "check_subscriptions",
            "analytics_cleanup",
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
