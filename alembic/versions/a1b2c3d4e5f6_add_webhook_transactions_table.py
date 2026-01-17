"""add_webhook_transactions_table

Revision ID: a1b2c3d4e5f6
Revises: 4a5c0507e2b4
Create Date: 2026-01-15 15:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "4a5c0507e2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create webhook_transactions table for RevenueCat webhook idempotency and audit."""
    op.create_table(
        "webhook_transactions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("transaction_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("processing_result", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id"),
    )
    op.create_index(
        "idx_webhook_transactions_transaction_id",
        "webhook_transactions",
        ["transaction_id"],
        unique=True,
    )
    op.create_index(
        "idx_webhook_transactions_event_type",
        "webhook_transactions",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "idx_webhook_transactions_user_id",
        "webhook_transactions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "idx_webhook_transactions_processed_at",
        "webhook_transactions",
        ["processed_at"],
        unique=False,
    )
    op.create_index(
        "idx_webhook_transactions_correlation_id",
        "webhook_transactions",
        ["correlation_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop webhook_transactions table."""
    op.drop_index(
        "idx_webhook_transactions_correlation_id", table_name="webhook_transactions"
    )
    op.drop_index(
        "idx_webhook_transactions_processed_at", table_name="webhook_transactions"
    )
    op.drop_index(
        "idx_webhook_transactions_user_id", table_name="webhook_transactions"
    )
    op.drop_index(
        "idx_webhook_transactions_event_type", table_name="webhook_transactions"
    )
    op.drop_index(
        "idx_webhook_transactions_transaction_id", table_name="webhook_transactions"
    )
    op.drop_table("webhook_transactions")
