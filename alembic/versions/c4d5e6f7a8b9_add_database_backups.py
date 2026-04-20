"""add database_backups + backup_settings tables

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-04-20 04:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BACKUP_STATUS_ENUM = sa.Enum("running", "ok", "failed", name="backup_status")
BACKUP_STORAGE_ENUM = sa.Enum("local", "r2", "both", name="backup_storage")


def upgrade() -> None:
    BACKUP_STATUS_ENUM.create(op.get_bind(), checkfirst=True)
    BACKUP_STORAGE_ENUM.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "database_backups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=True),
        sa.Column("duration_seconds", sa.Float, nullable=True),
        sa.Column("storage", BACKUP_STORAGE_ENUM, nullable=False, server_default="local"),
        sa.Column("r2_key", sa.String(512), nullable=True),
        sa.Column("local_path", sa.String(512), nullable=True),
        sa.Column("status", BACKUP_STATUS_ENUM, nullable=False, server_default="running"),
        sa.Column("triggered_by", sa.String(20), nullable=False, server_default="scheduler"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_database_backups_started_at",
        "database_backups",
        [sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_database_backups_status",
        "database_backups",
        ["status"],
    )

    op.create_table(
        "backup_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("interval_hours", sa.Integer, nullable=False, server_default="24"),
        sa.Column("retention_days", sa.Integer, nullable=False, server_default="7"),
        sa.Column(
            "enabled", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="backup_settings_single_row"),
    )
    op.execute("INSERT INTO backup_settings (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("backup_settings")
    op.drop_index("ix_database_backups_status", table_name="database_backups")
    op.drop_index("ix_database_backups_started_at", table_name="database_backups")
    op.drop_table("database_backups")
    BACKUP_STORAGE_ENUM.drop(op.get_bind(), checkfirst=True)
    BACKUP_STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
