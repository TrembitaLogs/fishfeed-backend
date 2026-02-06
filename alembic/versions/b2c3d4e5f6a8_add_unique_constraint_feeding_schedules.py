"""add_unique_constraint_feeding_schedules_fish_time

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-02-04 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a8"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove duplicate schedules and add unique constraint on (fish_id, time)."""

    # Step 1: Delete duplicate schedules, keeping only the oldest one per (fish_id, time).
    # This uses a window function to rank duplicates and deletes all but rank=1.
    op.execute(
        sa.text("""
            DELETE FROM feeding_schedules
            WHERE id IN (
                SELECT id FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY fish_id, time
                            ORDER BY created_at ASC, id ASC
                        ) as rn
                    FROM feeding_schedules
                ) ranked
                WHERE rn > 1
            )
        """)
    )

    # Step 2: Add unique constraint to prevent future duplicates.
    op.create_unique_constraint(
        "uq_feeding_schedules_fish_time",
        "feeding_schedules",
        ["fish_id", "time"],
    )


def downgrade() -> None:
    """Remove unique constraint (cannot restore deleted duplicates)."""
    op.drop_constraint(
        "uq_feeding_schedules_fish_time",
        "feeding_schedules",
        type_="unique",
    )
