"""add_soft_delete_to_feeding_events

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-01-27 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add deleted_at column and indexes for soft delete on feeding_events."""
    # Add deleted_at column
    op.add_column(
        "feeding_events",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Add index on deleted_at for soft delete queries
    op.create_index(
        "ix_feeding_events_deleted_at",
        "feeding_events",
        ["deleted_at"],
    )

    # Add partial index for active events (where deleted_at IS NULL)
    op.create_index(
        "idx_feeding_events_active",
        "feeding_events",
        ["aquarium_id", "scheduled_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Remove soft delete column and indexes from feeding_events."""
    op.drop_index("idx_feeding_events_active", table_name="feeding_events")
    op.drop_index("ix_feeding_events_deleted_at", table_name="feeding_events")
    op.drop_column("feeding_events", "deleted_at")
