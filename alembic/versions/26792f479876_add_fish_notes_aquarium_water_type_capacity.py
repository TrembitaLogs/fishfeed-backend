"""add_fish_notes_aquarium_water_type_capacity

Revision ID: 26792f479876
Revises: 826b8a5d851a
Create Date: 2026-03-03 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "26792f479876"
down_revision: str | Sequence[str] | None = "826b8a5d851a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add notes to fish, water_type and capacity to aquariums."""
    op.add_column("fish", sa.Column("notes", sa.String(length=500), nullable=True))
    op.add_column(
        "aquariums",
        sa.Column("water_type", sa.String(length=20), nullable=True, server_default="freshwater"),
    )
    op.add_column("aquariums", sa.Column("capacity", sa.Numeric(precision=8, scale=2), nullable=True))


def downgrade() -> None:
    """Remove capacity and water_type from aquariums, notes from fish."""
    op.drop_column("aquariums", "capacity")
    op.drop_column("aquariums", "water_type")
    op.drop_column("fish", "notes")
