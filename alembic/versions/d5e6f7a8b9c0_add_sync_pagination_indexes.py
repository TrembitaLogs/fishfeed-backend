"""add feeding_logs(aquarium_id,created_at) and aquariums(owner_id) indexes

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-06-27 00:00:00.000000

These indexes back the DB-level /sync pagination:
- idx_feeding_logs_aquarium_created supports the delta filter
  (aquarium_id IN (...) AND created_at >= since) and the deterministic
  ORDER BY (created_at, id) used when windowing feeding_logs.
- idx_aquariums_owner_id supports the ownership lookup
  (Aquarium.owner_id == user_id); Postgres does not auto-index FK columns.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_feeding_logs_aquarium_created",
        "feeding_logs",
        ["aquarium_id", "created_at"],
    )
    op.create_index(
        "idx_aquariums_owner_id",
        "aquariums",
        ["owner_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_aquariums_owner_id", table_name="aquariums")
    op.drop_index("idx_feeding_logs_aquarium_created", table_name="feeding_logs")
