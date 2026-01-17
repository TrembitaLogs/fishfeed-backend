"""add_throttling_fields_to_notification_preferences

Revision ID: 4a5c0507e2b4
Revises: 3694ba384fc9
Create Date: 2026-01-15 13:13:09.563903

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4a5c0507e2b4"
down_revision: Union[str, Sequence[str], None] = "3694ba384fc9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add global_opt_out and timezone fields to notification_preferences."""
    op.add_column(
        "notification_preferences",
        sa.Column(
            "global_opt_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "notification_preferences",
        sa.Column("timezone", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    """Remove global_opt_out and timezone fields from notification_preferences."""
    op.drop_column("notification_preferences", "timezone")
    op.drop_column("notification_preferences", "global_opt_out")
