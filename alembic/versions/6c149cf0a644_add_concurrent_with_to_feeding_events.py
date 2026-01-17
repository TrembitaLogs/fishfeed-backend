"""add_concurrent_with_to_feeding_events

Revision ID: 6c149cf0a644
Revises: 70253d72350b
Create Date: 2026-01-14 16:28:11.236307

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "6c149cf0a644"
down_revision: Union[str, Sequence[str], None] = "70253d72350b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add concurrent_with field to feeding_events table."""
    op.add_column(
        "feeding_events", sa.Column("concurrent_with", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        "fk_feeding_events_concurrent_with",
        "feeding_events",
        "feeding_events",
        ["concurrent_with"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Remove concurrent_with field from feeding_events table."""
    op.drop_constraint(
        "fk_feeding_events_concurrent_with", "feeding_events", type_="foreignkey"
    )
    op.drop_column("feeding_events", "concurrent_with")
