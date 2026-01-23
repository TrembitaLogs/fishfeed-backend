"""add_fish_id_to_feeding_events

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-01-23 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add fish_id and species_id columns to feeding_events table."""
    # Add fish_id as UUID FK to fish table
    op.add_column(
        "feeding_events", sa.Column("fish_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        "fk_feeding_events_fish_id",
        "feeding_events",
        "fish",
        ["fish_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Add species_id as string for direct species reference from mobile
    op.add_column(
        "feeding_events", sa.Column("species_id", sa.String(50), nullable=True)
    )


def downgrade() -> None:
    """Remove fish_id and species_id columns from feeding_events table."""
    op.drop_column("feeding_events", "species_id")
    op.drop_constraint(
        "fk_feeding_events_fish_id", "feeding_events", type_="foreignkey"
    )
    op.drop_column("feeding_events", "fish_id")
