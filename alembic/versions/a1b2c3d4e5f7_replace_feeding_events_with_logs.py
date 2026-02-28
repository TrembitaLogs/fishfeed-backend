"""replace_feeding_events_with_feeding_logs

Revision ID: a1b2c3d4e5f7
Revises: f6a7b8c9d0e1
Create Date: 2026-02-02 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f7"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace FeedingEvent with FeedingLog and refactor FeedingSchedule columns."""

    # Step 1: Create feeding_logs table (needed before data migration)
    op.create_table(
        "feeding_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("fish_id", sa.UUID(), nullable=False),
        sa.Column("aquarium_id", sa.UUID(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=False), nullable=False),
        sa.Column(
            "action",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column(
            "acted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("acted_by_user_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["feeding_schedules.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fish_id"],
            ["fish.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["aquarium_id"],
            ["aquariums.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["acted_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_feeding_logs_schedule_scheduled_for",
        ),
        sa.CheckConstraint(
            "action IN ('fed', 'skipped')",
            name="ck_feeding_logs_action",
        ),
    )
    op.create_index("idx_feeding_logs_aquarium_id", "feeding_logs", ["aquarium_id"])
    op.create_index("idx_feeding_logs_fish_id", "feeding_logs", ["fish_id"])
    op.create_index("idx_feeding_logs_scheduled_for", "feeding_logs", ["scheduled_for"])
    op.create_index("idx_feeding_logs_acted_at", "feeding_logs", ["acted_at"])

    # Step 2: Migrate completed feeding_events → feeding_logs
    # Only non-soft-deleted completed events with both schedule_id and fish_id present.
    # device_id is generated as uuid5 from event id (namespace = '6ba7b810-9dad-11d1-80b4-00c04fd430c8').
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    op.execute(
        sa.text("""
            INSERT INTO feeding_logs (
                id, schedule_id, fish_id, aquarium_id,
                scheduled_for, action, acted_at, acted_by_user_id,
                device_id, notes, created_at
            )
            SELECT
                fe.id,
                fe.schedule_id,
                fe.fish_id,
                fe.aquarium_id,
                fe.scheduled_at AT TIME ZONE 'UTC',
                'fed',
                COALESCE(fe.completed_at, fe.created_at),
                COALESCE(fe.completed_by, (
                    SELECT a.owner_id FROM aquariums a WHERE a.id = fe.aquarium_id
                )),
                uuid_generate_v5(
                    '6ba7b810-9dad-11d1-80b4-00c04fd430c8'::uuid,
                    fe.id::text
                ),
                NULL,
                fe.created_at
            FROM feeding_events fe
            WHERE fe.status = 'completed'
              AND fe.deleted_at IS NULL
              AND fe.schedule_id IS NOT NULL
              AND fe.fish_id IS NOT NULL
        """)
    )

    # Step 3: Delete all rows from feeding_schedules (old data is incompatible)
    op.execute(sa.text("DELETE FROM feeding_schedules"))

    # Step 4: Drop feeding_events table
    # Drop indexes first, then foreign keys, then the table
    op.drop_index("idx_feeding_events_active", table_name="feeding_events")
    op.drop_index("ix_feeding_events_deleted_at", table_name="feeding_events")
    op.drop_index("idx_feeding_events_pending", table_name="feeding_events")
    op.drop_index("idx_feeding_events_aquarium_scheduled", table_name="feeding_events")
    op.drop_index("ix_feeding_events_status", table_name="feeding_events")
    op.drop_table("feeding_events")

    # Step 5: Alter feeding_schedules — drop old columns
    op.drop_column("feeding_schedules", "times_per_day")
    op.drop_column("feeding_schedules", "scheduled_times")

    # Step 6: Add new columns to feeding_schedules
    # Table is empty at this point, so NOT NULL without default is safe for fish_id.
    op.add_column(
        "feeding_schedules",
        sa.Column(
            "fish_id",
            sa.UUID(),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_feeding_schedules_fish_id",
        "feeding_schedules",
        "fish",
        ["fish_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column(
        "feeding_schedules",
        sa.Column(
            "time",
            sa.Time(),
            nullable=False,
            server_default=sa.text("'09:00'"),
        ),
    )
    op.add_column(
        "feeding_schedules",
        sa.Column(
            "interval_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "feeding_schedules",
        sa.Column(
            "anchor_date",
            sa.Date(),
            nullable=False,
            server_default=sa.text("CURRENT_DATE"),
        ),
    )
    op.add_column(
        "feeding_schedules",
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "feeding_schedules",
        sa.Column(
            "created_by_user_id",
            sa.UUID(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_feeding_schedules_created_by_user_id",
        "feeding_schedules",
        "users",
        ["created_by_user_id"],
        ["id"],
    )

    # Step 7: Create indexes on feeding_schedules
    op.create_index("idx_feeding_schedules_fish_id", "feeding_schedules", ["fish_id"])
    op.create_index("idx_feeding_schedules_aquarium_id", "feeding_schedules", ["aquarium_id"])
    op.create_index(
        "idx_feeding_schedules_active",
        "feeding_schedules",
        ["active"],
        postgresql_where=sa.text("active = true"),
    )


def downgrade() -> None:
    """Restore FeedingEvent table and old FeedingSchedule columns."""

    # Remove new indexes from feeding_schedules
    op.drop_index("idx_feeding_schedules_active", table_name="feeding_schedules")
    op.drop_index("idx_feeding_schedules_aquarium_id", table_name="feeding_schedules")
    op.drop_index("idx_feeding_schedules_fish_id", table_name="feeding_schedules")

    # Remove new foreign keys
    op.drop_constraint(
        "fk_feeding_schedules_created_by_user_id",
        "feeding_schedules",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_feeding_schedules_fish_id",
        "feeding_schedules",
        type_="foreignkey",
    )

    # Remove new columns from feeding_schedules
    op.drop_column("feeding_schedules", "created_by_user_id")
    op.drop_column("feeding_schedules", "active")
    op.drop_column("feeding_schedules", "anchor_date")
    op.drop_column("feeding_schedules", "interval_days")
    op.drop_column("feeding_schedules", "time")
    op.drop_column("feeding_schedules", "fish_id")

    # Restore old columns on feeding_schedules
    op.add_column(
        "feeding_schedules",
        sa.Column(
            "times_per_day",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("2"),
        ),
    )
    op.add_column(
        "feeding_schedules",
        sa.Column(
            "scheduled_times",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Recreate feeding_events table
    op.create_table(
        "feeding_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("aquarium_id", sa.UUID(), nullable=False),
        sa.Column("schedule_id", sa.UUID(), nullable=True),
        sa.Column("fish_id", sa.UUID(), nullable=True),
        sa.Column("species_id", sa.String(length=50), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.UUID(), nullable=True),
        sa.Column("client_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("concurrent_with", sa.UUID(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["aquarium_id"],
            ["aquariums.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["feeding_schedules.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["fish_id"],
            ["fish.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["completed_by"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["concurrent_with"],
            ["feeding_events.id"],
            name="fk_feeding_events_concurrent_with",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feeding_events_status", "feeding_events", ["status"])
    op.create_index(
        "idx_feeding_events_aquarium_scheduled",
        "feeding_events",
        ["aquarium_id", "scheduled_at"],
    )
    op.create_index(
        "idx_feeding_events_pending",
        "feeding_events",
        ["status"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_feeding_events_deleted_at",
        "feeding_events",
        ["deleted_at"],
    )
    op.create_index(
        "idx_feeding_events_active",
        "feeding_events",
        ["aquarium_id", "scheduled_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Drop feeding_logs table
    op.drop_index("idx_feeding_logs_acted_at", table_name="feeding_logs")
    op.drop_index("idx_feeding_logs_scheduled_for", table_name="feeding_logs")
    op.drop_index("idx_feeding_logs_fish_id", table_name="feeding_logs")
    op.drop_index("idx_feeding_logs_aquarium_id", table_name="feeding_logs")
    op.drop_table("feeding_logs")
