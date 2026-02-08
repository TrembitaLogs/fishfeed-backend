"""Analytics cleanup background jobs for GDPR compliance.

This module provides scheduled jobs for:
- Anonymizing analytics events older than configured days (default 30)
- Deleting analytics events older than retention period (default 90 days)

Usage:
    # Run as standalone module
    python -m app.jobs.analytics_cleanup

    # Dry-run mode (no changes)
    python -m app.jobs.analytics_cleanup --dry-run

    # Force run specific job
    python -m app.jobs.analytics_cleanup --job=anonymize
    python -m app.jobs.analytics_cleanup --job=retention
"""

import argparse
import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, func, select

from app.config import get_settings
from app.database import async_session_maker
from app.models.analytics import AnalyticsEvent

logger = structlog.get_logger(__name__)
settings = get_settings()

# PII patterns to remove from event properties
PII_PATTERNS = [
    (re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"), "[EMAIL_REDACTED]"),
    (re.compile(r"\b\d{10,15}\b"), "[PHONE_REDACTED]"),
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE_REDACTED]"),
    (re.compile(r"\+\d{1,3}[-.\s]?\d{1,14}"), "[PHONE_REDACTED]"),
]

# Keys commonly containing PII in properties
PII_KEYS = {"email", "phone", "name", "first_name", "last_name", "full_name", "address"}


def remove_pii_from_properties(properties: dict[str, Any] | None) -> dict[str, Any]:
    """Remove PII from event properties dictionary.

    Args:
        properties: Event properties dictionary.

    Returns:
        Cleaned properties with PII redacted.
    """
    if not properties:
        return {}

    cleaned: dict[str, Any] = {}
    for key, value in properties.items():
        key_lower = key.lower()

        # Redact known PII keys
        if key_lower in PII_KEYS:
            cleaned[key] = "[PII_REDACTED]"
            continue

        # Check string values for PII patterns
        if isinstance(value, str):
            cleaned_value = value
            for pattern, replacement in PII_PATTERNS:
                cleaned_value = pattern.sub(replacement, cleaned_value)
            cleaned[key] = cleaned_value
        elif isinstance(value, dict):
            cleaned[key] = remove_pii_from_properties(value)
        elif isinstance(value, list):
            cleaned[key] = [
                remove_pii_from_properties(item) if isinstance(item, dict)
                else item
                for item in value
            ]
        else:
            cleaned[key] = value

    return cleaned


async def anonymize_old_events_job(dry_run: bool = False) -> dict:
    """Anonymize analytics events older than configured threshold.

    Finds events where created_at < NOW() - ANONYMIZE_AFTER_DAYS
    and anonymized_at IS NULL, then:
    - Sets user_id = NULL
    - Sets ip_hash = 'anonymized'
    - Sets anonymized_at = NOW()
    - Removes PII from properties

    Args:
        dry_run: If True, only report what would be done without making changes.

    Returns:
        Dict with job statistics.
    """
    anonymize_days = settings.ANALYTICS_ANONYMIZE_AFTER_DAYS
    batch_size = settings.ANALYTICS_CLEANUP_BATCH_SIZE
    cutoff_date = datetime.now(UTC) - timedelta(days=anonymize_days)

    logger.info(
        f"Starting anonymize_old_events_job (dry_run={dry_run}, "
        f"cutoff={cutoff_date.date()}, batch_size={batch_size})"
    )

    total_anonymized = 0
    total_batches = 0

    async with async_session_maker() as db:
        while True:
            # Select batch of events to anonymize
            stmt = (
                select(AnalyticsEvent)
                .where(AnalyticsEvent.created_at < cutoff_date)
                .where(AnalyticsEvent.anonymized_at.is_(None))
                .limit(batch_size)
            )
            result = await db.execute(stmt)
            events = list(result.scalars().all())

            if not events:
                break

            total_batches += 1

            if dry_run:
                total_anonymized += len(events)
                logger.info(
                    f"[DRY-RUN] Would anonymize batch {total_batches}: {len(events)} events"
                )
                # In dry-run, we need to mark them somehow to avoid infinite loop
                # Just break after first batch to show count
                break

            # Anonymize each event
            now = datetime.now(UTC)
            for event in events:
                event.user_id = None
                event.ip_hash = "anonymized"
                event.anonymized_at = now
                event.properties = remove_pii_from_properties(event.properties)

            await db.commit()
            total_anonymized += len(events)

            logger.info(
                f"Anonymized batch {total_batches}: {len(events)} events "
                f"(total: {total_anonymized})"
            )

    # Get remaining count for dry-run reporting
    remaining_count = 0
    if dry_run:
        async with async_session_maker() as db:
            count_stmt = (
                select(func.count())
                .select_from(AnalyticsEvent)
                .where(AnalyticsEvent.created_at < cutoff_date)
                .where(AnalyticsEvent.anonymized_at.is_(None))
            )
            result = await db.execute(count_stmt)
            remaining_count = result.scalar_one()  # type: ignore[assignment]

    stats = {
        "job": "anonymize_old_events",
        "dry_run": dry_run,
        "cutoff_date": cutoff_date.isoformat(),
        "anonymize_after_days": anonymize_days,
        "batch_size": batch_size,
        "total_anonymized": total_anonymized if not dry_run else 0,
        "would_anonymize": remaining_count if dry_run else 0,
        "batches_processed": total_batches,
    }

    logger.info(f"anonymize_old_events_job completed: {stats}")
    return stats


async def delete_old_events_job(dry_run: bool = False) -> dict:
    """Delete analytics events older than retention period.

    Deletes events where created_at < NOW() - RETENTION_DAYS.
    Uses batch deletion to avoid database locks.

    Args:
        dry_run: If True, only report what would be done without making changes.

    Returns:
        Dict with job statistics.
    """
    retention_days = settings.ANALYTICS_RETENTION_DAYS
    batch_size = settings.ANALYTICS_CLEANUP_BATCH_SIZE
    cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)

    logger.info(
        f"Starting delete_old_events_job (dry_run={dry_run}, "
        f"cutoff={cutoff_date.date()}, batch_size={batch_size})"
    )

    total_deleted = 0
    total_batches = 0

    async with async_session_maker() as db:
        # First, get total count for statistics
        count_stmt = (
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(AnalyticsEvent.created_at < cutoff_date)
        )
        result = await db.execute(count_stmt)
        total_count = result.scalar_one()

        if dry_run:
            logger.info(f"[DRY-RUN] Would delete {total_count} events older than {cutoff_date.date()}")
            return {
                "job": "delete_old_events",
                "dry_run": True,
                "cutoff_date": cutoff_date.isoformat(),
                "retention_days": retention_days,
                "batch_size": batch_size,
                "total_deleted": 0,
                "would_delete": total_count,
                "batches_processed": 0,
            }

        # Batch delete using subquery to get IDs
        while True:
            # Get batch of IDs to delete
            id_stmt = (
                select(AnalyticsEvent.id)
                .where(AnalyticsEvent.created_at < cutoff_date)
                .limit(batch_size)
            )
            id_result = await db.execute(id_stmt)
            ids_to_delete = [row[0] for row in id_result.all()]

            if not ids_to_delete:
                break

            total_batches += 1

            # Delete the batch
            delete_stmt = delete(AnalyticsEvent).where(AnalyticsEvent.id.in_(ids_to_delete))
            await db.execute(delete_stmt)
            await db.commit()

            total_deleted += len(ids_to_delete)

            logger.info(
                f"Deleted batch {total_batches}: {len(ids_to_delete)} events "
                f"(total: {total_deleted})"
            )

    stats = {
        "job": "delete_old_events",
        "dry_run": dry_run,
        "cutoff_date": cutoff_date.isoformat(),
        "retention_days": retention_days,
        "batch_size": batch_size,
        "total_deleted": total_deleted,
        "would_delete": 0,
        "batches_processed": total_batches,
    }

    logger.info(f"delete_old_events_job completed: {stats}")
    return stats


async def analytics_cleanup_job(dry_run: bool = False) -> dict:
    """Run both anonymization and retention jobs.

    This is the main scheduled job that runs daily.

    Args:
        dry_run: If True, only report what would be done.

    Returns:
        Combined statistics from both jobs.
    """
    logger.info(f"Starting analytics_cleanup_job (dry_run={dry_run})")

    anonymize_stats = await anonymize_old_events_job(dry_run=dry_run)
    retention_stats = await delete_old_events_job(dry_run=dry_run)

    combined_stats = {
        "job": "analytics_cleanup",
        "dry_run": dry_run,
        "timestamp": datetime.now(UTC).isoformat(),
        "anonymization": anonymize_stats,
        "retention": retention_stats,
    }

    logger.info(f"analytics_cleanup_job completed: {combined_stats}")
    return combined_stats


async def run_job(job_name: str | None = None, dry_run: bool = False) -> dict:
    """Run specified analytics cleanup job.

    Args:
        job_name: Job to run ('anonymize', 'retention', or None for both).
        dry_run: If True, only report what would be done.

    Returns:
        Job statistics.
    """
    if job_name == "anonymize":
        return await anonymize_old_events_job(dry_run=dry_run)
    elif job_name == "retention":
        return await delete_old_events_job(dry_run=dry_run)
    else:
        return await analytics_cleanup_job(dry_run=dry_run)


def main() -> None:
    """CLI entry point for analytics cleanup."""
    parser = argparse.ArgumentParser(description="FishFeed analytics cleanup jobs")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run cleanup immediately (same as not using --dry-run)",
    )
    parser.add_argument(
        "--job",
        type=str,
        choices=["anonymize", "retention"],
        help="Specific job to run (default: run both)",
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

    # Determine dry_run mode
    dry_run = args.dry_run and not args.force

    # Run the job
    result = asyncio.run(run_job(args.job, dry_run=dry_run))

    # Print summary
    print("\n" + "=" * 60)
    print("ANALYTICS CLEANUP SUMMARY")
    print("=" * 60)

    if dry_run:
        print("MODE: DRY-RUN (no changes made)")
    else:
        print("MODE: LIVE (changes applied)")

    if "anonymization" in result:
        print("\nAnonymization:")
        anon = result["anonymization"]
        if dry_run:
            print(f"  Would anonymize: {anon.get('would_anonymize', 0)} events")
        else:
            print(f"  Anonymized: {anon.get('total_anonymized', 0)} events")
        print(f"  Cutoff date: {anon.get('cutoff_date', 'N/A')}")

        print("\nRetention:")
        ret = result["retention"]
        if dry_run:
            print(f"  Would delete: {ret.get('would_delete', 0)} events")
        else:
            print(f"  Deleted: {ret.get('total_deleted', 0)} events")
        print(f"  Cutoff date: {ret.get('cutoff_date', 'N/A')}")
    else:
        # Single job result
        if dry_run:
            print(f"Would process: {result.get('would_anonymize', 0) + result.get('would_delete', 0)} events")
        else:
            print(f"Processed: {result.get('total_anonymized', 0) + result.get('total_deleted', 0)} events")
        print(f"Batches: {result.get('batches_processed', 0)}")

    print("=" * 60)


if __name__ == "__main__":
    main()
