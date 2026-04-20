"""Database backup service.

Runs `pg_dump` against the configured PostgreSQL database, stores the dump
locally under ``BACKUP_LOCAL_DIR`` and uploads a copy to the R2
``BACKUP_R2_BUCKET_NAME`` bucket. Retention is enforced after every successful
run: both local files and remote objects older than ``retention_days`` (from
``BackupSettings``) are removed.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import aioboto3
import structlog
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.database import async_session_maker
from app.models.database_backup import (
    BackupSettings,
    BackupStatus,
    BackupStorage,
    DatabaseBackup,
)

logger = structlog.get_logger(__name__)


class BackupError(Exception):
    """Raised when a backup operation cannot complete."""


def _parse_db_url(url: str) -> dict[str, str]:
    """Extract connection pieces from a SQLAlchemy asyncpg URL."""
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
        "database": (parsed.path or "/fishfeed").lstrip("/"),
    }


def _backup_filename(now: datetime) -> str:
    return f"fishfeed_backup_{now.strftime('%Y%m%d_%H%M%S')}.dump"


def _r2_key(filename: str) -> str:
    return f"pg/{filename}"


def _s3_session() -> aioboto3.Session:
    return aioboto3.Session()


def _s3_client_config(settings: Settings) -> dict[str, str | None]:
    return {
        "service_name": "s3",
        "endpoint_url": settings.S3_ENDPOINT_URL,
        "aws_access_key_id": settings.S3_ACCESS_KEY,
        "aws_secret_access_key": settings.S3_SECRET_KEY,
        "region_name": settings.S3_REGION,
    }


async def get_or_create_settings() -> BackupSettings:
    """Return the single ``BackupSettings`` row, creating it if missing."""
    async with async_session_maker() as session:
        result = await session.execute(select(BackupSettings).where(BackupSettings.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            row = BackupSettings(id=1)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row


async def update_settings(
    *, interval_hours: int, retention_days: int, enabled: bool
) -> BackupSettings:
    if interval_hours < 1 or interval_hours > 168:
        raise ValueError("interval_hours must be between 1 and 168")
    if retention_days < 1 or retention_days > 365:
        raise ValueError("retention_days must be between 1 and 365")

    async with async_session_maker() as session:
        result = await session.execute(select(BackupSettings).where(BackupSettings.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            row = BackupSettings(id=1)
            session.add(row)
        row.interval_hours = interval_hours
        row.retention_days = retention_days
        row.enabled = enabled
        await session.commit()
        await session.refresh(row)
        return row


async def run_backup(*, triggered_by: str = "scheduler") -> DatabaseBackup:
    """Execute pg_dump, upload to R2, record the result.

    Returns the committed ``DatabaseBackup`` record in either OK or FAILED state.
    Retention cleanup is invoked after a successful run.
    """
    settings = get_settings()
    started = datetime.now(UTC)
    filename = _backup_filename(started)
    local_dir = Path(settings.BACKUP_LOCAL_DIR)
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / filename

    record = DatabaseBackup(
        filename=filename,
        storage=BackupStorage.LOCAL,
        status=BackupStatus.RUNNING,
        triggered_by=triggered_by,
        started_at=started,
        local_path=str(local_path),
    )
    async with async_session_maker() as session:
        session.add(record)
        await session.commit()
        await session.refresh(record)
        record_id = record.id

    try:
        await _run_pg_dump(local_path)
        size = local_path.stat().st_size

        r2_key: str | None = None
        storage = BackupStorage.LOCAL
        if settings.S3_ENDPOINT_URL:
            r2_key = _r2_key(filename)
            await _upload_to_r2(local_path, r2_key)
            storage = BackupStorage.BOTH

        completed = datetime.now(UTC)
        duration = (completed - started).total_seconds()

        async with async_session_maker() as session:
            existing = await session.get(DatabaseBackup, record_id)
            if existing is None:
                raise BackupError("Backup record vanished mid-run")
            existing.size_bytes = size
            existing.duration_seconds = duration
            existing.storage = storage
            existing.r2_key = r2_key
            existing.status = BackupStatus.OK
            existing.completed_at = completed
            await session.commit()
            await session.refresh(existing)
            result_record = existing

        logger.info(
            "Database backup completed",
            filename=filename,
            size_bytes=size,
            duration_seconds=duration,
            storage=storage.value,
        )

        await cleanup_expired_backups()
        return result_record
    except Exception as exc:
        logger.exception("Database backup failed", filename=filename)
        completed = datetime.now(UTC)
        duration = (completed - started).total_seconds()
        async with async_session_maker() as session:
            existing = await session.get(DatabaseBackup, record_id)
            if existing is not None:
                existing.status = BackupStatus.FAILED
                existing.error_message = str(exc)[:4000]
                existing.completed_at = completed
                existing.duration_seconds = duration
                await session.commit()
                await session.refresh(existing)
                result_record = existing
            else:
                raise
        if local_path.exists():
            try:
                local_path.unlink()
            except OSError:
                pass
        return result_record


async def _run_pg_dump(local_path: Path) -> None:
    settings = get_settings()
    conn = _parse_db_url(settings.DATABASE_URL)
    env = os.environ.copy()
    env["PGPASSWORD"] = conn["password"]

    cmd = [
        settings.BACKUP_PG_DUMP_PATH,
        "-h",
        conn["host"],
        "-p",
        conn["port"],
        "-U",
        conn["user"],
        "-d",
        conn["database"],
        "-Fc",
        "-Z",
        "6",
        "--no-owner",
        "--no-privileges",
        "-f",
        str(local_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip() or "pg_dump failed"
        raise BackupError(f"pg_dump exited {proc.returncode}: {msg}")


async def _upload_to_r2(local_path: Path, key: str) -> None:
    settings = get_settings()
    session = _s3_session()
    async with session.client(**_s3_client_config(settings)) as s3:
        with local_path.open("rb") as fh:
            await s3.put_object(
                Bucket=settings.BACKUP_R2_BUCKET_NAME,
                Key=key,
                Body=fh.read(),
                ContentType="application/octet-stream",
            )


async def cleanup_expired_backups() -> dict[str, int]:
    """Delete local files, R2 objects, and DB rows past ``retention_days``.

    Returns a summary of how many items were removed in each location.
    """
    cfg = await get_or_create_settings()
    cutoff = datetime.now(UTC) - timedelta(days=cfg.retention_days)

    local_removed = 0
    r2_removed = 0
    rows_removed = 0
    settings = get_settings()

    async with async_session_maker() as session:
        result = await session.execute(
            select(DatabaseBackup).where(DatabaseBackup.started_at < cutoff)
        )
        old = list(result.scalars())

        r2_keys = [r.r2_key for r in old if r.r2_key]
        if r2_keys and settings.S3_ENDPOINT_URL:
            s3_session = _s3_session()
            async with s3_session.client(**_s3_client_config(settings)) as s3:
                for key in r2_keys:
                    try:
                        await s3.delete_object(
                            Bucket=settings.BACKUP_R2_BUCKET_NAME, Key=key
                        )
                        r2_removed += 1
                    except Exception as exc:
                        logger.warning(
                            "Failed to delete R2 backup", key=key, error=str(exc)
                        )

        for record in old:
            if record.local_path:
                path = Path(record.local_path)
                if path.exists():
                    try:
                        path.unlink()
                        local_removed += 1
                    except OSError as exc:
                        logger.warning(
                            "Failed to delete local backup",
                            path=str(path),
                            error=str(exc),
                        )
            await session.delete(record)
            rows_removed += 1
        await session.commit()

    return {
        "local_removed": local_removed,
        "r2_removed": r2_removed,
        "rows_removed": rows_removed,
    }


async def get_backup_stats() -> dict[str, object]:
    """Aggregate dashboard data: last backup, storage counts, total size."""
    async with async_session_maker() as session:
        last_q = await session.execute(
            select(DatabaseBackup).order_by(DatabaseBackup.started_at.desc()).limit(1)
        )
        last_backup = last_q.scalar_one_or_none()

        count_q = await session.execute(
            select(
                func.count(DatabaseBackup.id),
                func.coalesce(func.sum(DatabaseBackup.size_bytes), 0),
            ).where(DatabaseBackup.status == BackupStatus.OK)
        )
        total_count, total_bytes = count_q.one()

    settings_row = await get_or_create_settings()

    return {
        "last_backup": last_backup,
        "total_count": int(total_count or 0),
        "total_bytes": int(total_bytes or 0),
        "settings": settings_row,
    }
