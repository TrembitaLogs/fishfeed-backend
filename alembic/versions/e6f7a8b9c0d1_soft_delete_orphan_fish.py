"""soft-delete orphan fish + deactivate schedules under soft-deleted aquariums

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-06-30 00:00:00.000000

One-time data cleanup. Before aquarium deletes cascaded to their children,
deleting an aquarium left its fish alive (orphans) and their schedules active
on the server. Those orphans are invisible to clients (the /sync download is
scoped to active aquariums) but accumulate as stale rows. This backfills the
cascade for already-deleted aquariums:
- soft-delete fish that are still alive under a soft-deleted aquarium;
- deactivate those aquariums' still-active schedules.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE fish
        SET deleted_at = now(), updated_at = now()
        WHERE deleted_at IS NULL
          AND aquarium_id IN (
              SELECT id FROM aquariums WHERE deleted_at IS NOT NULL
          )
        """
    )
    op.execute(
        """
        UPDATE feeding_schedules
        SET active = false
        WHERE active = true
          AND aquarium_id IN (
              SELECT id FROM aquariums WHERE deleted_at IS NOT NULL
          )
        """
    )


def downgrade() -> None:
    # Data backfill: backfilled rows are indistinguishable from genuinely
    # deleted ones, so this is intentionally not reversible.
    pass
