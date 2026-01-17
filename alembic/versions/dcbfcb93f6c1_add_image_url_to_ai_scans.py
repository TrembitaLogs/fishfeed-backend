"""add_image_url_to_ai_scans

Revision ID: dcbfcb93f6c1
Revises: 6c149cf0a644
Create Date: 2026-01-15 09:07:31.835216

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "dcbfcb93f6c1"
down_revision: Union[str, Sequence[str], None] = "6c149cf0a644"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "ai_scans",
        sa.Column("image_url", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("ai_scans", "image_url")
