"""add_analytics_events_table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-01-15 16:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create analytics_events table for product analytics."""
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column(
            "properties",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "device_info",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("ip_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Single column indexes
    op.create_index(
        op.f("ix_analytics_events_user_id"),
        "analytics_events",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analytics_events_event_type"),
        "analytics_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analytics_events_created_at"),
        "analytics_events",
        ["created_at"],
        unique=False,
    )
    # Composite indexes for common query patterns
    op.create_index(
        "idx_analytics_events_user_created",
        "analytics_events",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_analytics_events_type_created",
        "analytics_events",
        ["event_type", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop analytics_events table."""
    op.drop_index(
        "idx_analytics_events_type_created", table_name="analytics_events"
    )
    op.drop_index(
        "idx_analytics_events_user_created", table_name="analytics_events"
    )
    op.drop_index(
        op.f("ix_analytics_events_created_at"), table_name="analytics_events"
    )
    op.drop_index(
        op.f("ix_analytics_events_event_type"), table_name="analytics_events"
    )
    op.drop_index(
        op.f("ix_analytics_events_user_id"), table_name="analytics_events"
    )
    op.drop_table("analytics_events")
