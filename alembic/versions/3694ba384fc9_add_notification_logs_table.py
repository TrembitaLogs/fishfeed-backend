"""add_notification_logs_table

Revision ID: 3694ba384fc9
Revises: dcbfcb93f6c1
Create Date: 2026-01-15 11:49:58.395002

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3694ba384fc9"
down_revision: Union[str, Sequence[str], None] = "dcbfcb93f6c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "notification_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("notification_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_logs_created_at"),
        "notification_logs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_logs_notification_type"),
        "notification_logs",
        ["notification_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_logs_user_id"),
        "notification_logs",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_notification_logs_user_id"), table_name="notification_logs")
    op.drop_index(
        op.f("ix_notification_logs_notification_type"), table_name="notification_logs"
    )
    op.drop_index(
        op.f("ix_notification_logs_created_at"), table_name="notification_logs"
    )
    op.drop_table("notification_logs")
