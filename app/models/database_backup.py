"""Database backup tracking models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BackupStatus(str, enum.Enum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


class BackupStorage(str, enum.Enum):
    LOCAL = "local"
    R2 = "r2"
    BOTH = "both"


class DatabaseBackup(Base, TimestampMixin):
    __tablename__ = "database_backups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    storage: Mapped[BackupStorage] = mapped_column(
        Enum(BackupStorage, name="backup_storage"),
        nullable=False,
        default=BackupStorage.LOCAL,
    )
    r2_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    local_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[BackupStatus] = mapped_column(
        Enum(BackupStatus, name="backup_status"),
        nullable=False,
        default=BackupStatus.RUNNING,
    )
    triggered_by: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduler"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BackupSettings(Base, TimestampMixin):
    """Single-row configuration for the backup scheduler.

    There is always exactly one row with id=1. Seeded by the initial migration.
    """

    __tablename__ = "backup_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24
    )
    retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7
    )
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
