"""Tests for the database-backup service."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database_backup import (
    BackupSettings,
    BackupStatus,
    BackupStorage,
    DatabaseBackup,
)
from app.services import backup_service


async def _reset_tables(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM database_backups"))
    await session.execute(text("DELETE FROM backup_settings"))
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_get_or_create_settings_seeds_row(async_session: AsyncSession) -> None:
    await _reset_tables(async_session)

    row = await backup_service.get_or_create_settings()

    assert row.id == 1
    assert row.interval_hours == 24
    assert row.retention_days == 7
    assert row.enabled is True


@pytest.mark.asyncio(loop_scope="session")
async def test_update_settings_validates_bounds(async_session: AsyncSession) -> None:
    await _reset_tables(async_session)
    await backup_service.get_or_create_settings()

    with pytest.raises(ValueError):
        await backup_service.update_settings(
            interval_hours=0, retention_days=7, enabled=True
        )
    with pytest.raises(ValueError):
        await backup_service.update_settings(
            interval_hours=24, retention_days=0, enabled=True
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_update_settings_persists(async_session: AsyncSession) -> None:
    await _reset_tables(async_session)
    await backup_service.get_or_create_settings()

    updated = await backup_service.update_settings(
        interval_hours=12, retention_days=30, enabled=False
    )

    assert updated.interval_hours == 12
    assert updated.retention_days == 30
    assert updated.enabled is False


@pytest.mark.asyncio(loop_scope="session")
async def test_get_backup_stats_empty(async_session: AsyncSession) -> None:
    await _reset_tables(async_session)

    stats = await backup_service.get_backup_stats()

    assert stats["last_backup"] is None
    assert stats["total_count"] == 0
    assert stats["total_bytes"] == 0
    assert isinstance(stats["settings"], BackupSettings)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_backup_stats_with_records(async_session: AsyncSession) -> None:
    await _reset_tables(async_session)
    await backup_service.get_or_create_settings()

    older = DatabaseBackup(
        filename="older.dump",
        size_bytes=1000,
        duration_seconds=1.5,
        storage=BackupStorage.LOCAL,
        status=BackupStatus.OK,
        started_at=datetime.now(UTC) - timedelta(hours=2),
        completed_at=datetime.now(UTC) - timedelta(hours=2),
    )
    newer = DatabaseBackup(
        filename="newer.dump",
        size_bytes=2000,
        duration_seconds=1.8,
        storage=BackupStorage.BOTH,
        status=BackupStatus.OK,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    async_session.add_all([older, newer])
    await async_session.commit()

    stats = await backup_service.get_backup_stats()

    assert stats["total_count"] == 2
    assert stats["total_bytes"] == 3000
    last = stats["last_backup"]
    assert last is not None
    assert last.filename == "newer.dump"


@pytest.mark.asyncio(loop_scope="session")
async def test_cleanup_expired_backups_removes_old(async_session: AsyncSession) -> None:
    await _reset_tables(async_session)
    await backup_service.update_settings(
        interval_hours=24, retention_days=1, enabled=True
    )

    old = DatabaseBackup(
        filename="old.dump",
        size_bytes=1000,
        storage=BackupStorage.LOCAL,
        status=BackupStatus.OK,
        started_at=datetime.now(UTC) - timedelta(days=5),
        completed_at=datetime.now(UTC) - timedelta(days=5),
    )
    fresh = DatabaseBackup(
        filename="fresh.dump",
        size_bytes=2000,
        storage=BackupStorage.LOCAL,
        status=BackupStatus.OK,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    async_session.add_all([old, fresh])
    await async_session.commit()

    summary = await backup_service.cleanup_expired_backups()

    assert summary["rows_removed"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_run_backup_records_failure(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _reset_tables(async_session)
    await backup_service.get_or_create_settings()

    settings = backup_service.get_settings()
    monkeypatch.setattr(settings, "BACKUP_LOCAL_DIR", str(tmp_path))

    async def _fail(_path: object) -> None:
        raise backup_service.BackupError("pg_dump missing")

    monkeypatch.setattr(backup_service, "_run_pg_dump", _fail)

    record = await backup_service.run_backup(triggered_by="manual")

    assert record.status == BackupStatus.FAILED
    assert record.error_message is not None
    assert "pg_dump missing" in record.error_message


@pytest.mark.asyncio(loop_scope="session")
async def test_run_backup_skips_r2_when_not_configured(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _reset_tables(async_session)
    await backup_service.get_or_create_settings()

    settings = backup_service.get_settings()
    monkeypatch.setattr(settings, "BACKUP_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", None)

    async def _fake_pg_dump(path: Path) -> None:
        path.write_bytes(b"fake dump bytes")

    monkeypatch.setattr(backup_service, "_run_pg_dump", _fake_pg_dump)

    upload_mock = AsyncMock()
    monkeypatch.setattr(backup_service, "_upload_to_r2", upload_mock)

    record = await backup_service.run_backup(triggered_by="manual")

    assert record.status == BackupStatus.OK
    assert record.storage == BackupStorage.LOCAL
    assert record.size_bytes == len(b"fake dump bytes")
    upload_mock.assert_not_called()
