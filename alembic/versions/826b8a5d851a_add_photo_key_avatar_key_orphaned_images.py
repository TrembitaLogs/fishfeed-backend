"""add_photo_key_avatar_key_orphaned_images

Revision ID: 826b8a5d851a
Revises: 22db71ef7678
Create Date: 2026-02-26 14:34:03.259392

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "826b8a5d851a"
down_revision: Union[str, Sequence[str], None] = "22db71ef7678"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add photo_key to aquariums/fish, rename avatar_url to avatar_key, create orphaned_images."""
    # Create orphaned_images table for garbage collection of replaced/removed photos
    op.create_table(
        "orphaned_images",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("old_key", sa.String(length=500), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("orphaned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orphaned_images_orphaned_at", "orphaned_images", ["orphaned_at"], unique=False)

    # Add photo_key to aquariums and fish
    op.add_column("aquariums", sa.Column("photo_key", sa.String(length=500), nullable=True))
    op.add_column("fish", sa.Column("photo_key", sa.String(length=500), nullable=True))

    # Rename avatar_url -> avatar_key in users (also changes type from Text to String(500))
    op.add_column("users", sa.Column("avatar_key", sa.String(length=500), nullable=True))
    op.drop_column("users", "avatar_url")


def downgrade() -> None:
    """Reverse: drop photo_key, rename avatar_key back to avatar_url, drop orphaned_images."""
    # Restore avatar_url in users
    op.add_column("users", sa.Column("avatar_url", sa.TEXT(), autoincrement=False, nullable=True))
    op.drop_column("users", "avatar_key")

    # Remove photo_key from aquariums and fish
    op.drop_column("fish", "photo_key")
    op.drop_column("aquariums", "photo_key")

    # Drop orphaned_images table
    op.drop_index("ix_orphaned_images_orphaned_at", table_name="orphaned_images")
    op.drop_table("orphaned_images")
