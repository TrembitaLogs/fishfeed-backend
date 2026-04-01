"""add sync_dead_letters table

Revision ID: a2b3c4d5e6f7
Revises: 26792f479876
Create Date: 2026-04-01 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "26792f479876"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_dead_letters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("error_message", sa.Text, nullable=False),
        sa.Column("error_type", sa.String(100), nullable=False),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_sync_dead_letters_user_id",
        "sync_dead_letters",
        ["user_id"],
    )
    op.create_index(
        "ix_sync_dead_letters_unresolved",
        "sync_dead_letters",
        ["resolved_at"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sync_dead_letters_unresolved", table_name="sync_dead_letters")
    op.drop_index("ix_sync_dead_letters_user_id", table_name="sync_dead_letters")
    op.drop_table("sync_dead_letters")
