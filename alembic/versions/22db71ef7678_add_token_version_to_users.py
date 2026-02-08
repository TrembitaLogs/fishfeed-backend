"""add_token_version_to_users

Revision ID: 22db71ef7678
Revises: b2c3d4e5f6a8
Create Date: 2026-02-07 20:18:46.839226

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "22db71ef7678"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("token_version", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "token_version")
