"""add composite index (active, aquarium_id) on feeding_schedules

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-04-01 03:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_feeding_schedules_active_aquarium",
        "feeding_schedules",
        ["active", "aquarium_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_feeding_schedules_active_aquarium", table_name="feeding_schedules")
