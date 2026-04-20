"""Scheduled database backup job.

Runs every ``BACKUP_CHECK_INTERVAL_MINUTES`` and decides whether it is time to
create a new backup based on the current ``BackupSettings`` row. Keeping the
cadence in the DB (not the cron trigger) lets admins change the interval from
the UI without restarting the worker.
"""

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from app.database import async_session_maker
from app.models.database_backup import BackupStatus, DatabaseBackup
from app.services.backup_service import get_or_create_settings, run_backup

logger = structlog.get_logger(__name__)


async def backup_database_job() -> dict[str, object]:
    """Check settings and trigger a backup if the interval has elapsed."""
    settings_row = await get_or_create_settings()
    if not settings_row.enabled:
        return {"job": "backup_database", "skipped": True, "reason": "disabled"}

    async with async_session_maker() as session:
        result = await session.execute(
            select(DatabaseBackup)
            .where(DatabaseBackup.status == BackupStatus.OK)
            .order_by(DatabaseBackup.started_at.desc())
            .limit(1)
        )
        last_ok = result.scalar_one_or_none()

    now = datetime.now(UTC)
    if last_ok is not None:
        due_at = last_ok.started_at + timedelta(hours=settings_row.interval_hours)
        if now < due_at:
            return {
                "job": "backup_database",
                "skipped": True,
                "reason": "interval not elapsed",
                "due_at": due_at.isoformat(),
            }

    record = await run_backup(triggered_by="scheduler")
    return {
        "job": "backup_database",
        "backup_id": str(record.id),
        "status": record.status.value,
        "size_bytes": record.size_bytes,
    }
