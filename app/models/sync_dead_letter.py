"""Dead-letter model for failed sync changes."""

import uuid

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SyncDeadLetter(Base):
    """Stores sync changes that failed processing for later analysis or retry.

    When a sync change fails (validation, constraint violation, unexpected error),
    the change payload is persisted here instead of being silently dropped.
    """

    __tablename__ = "sync_dead_letters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)
    resolved_at: Mapped[None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_sync_dead_letters_user_id", "user_id"),
        Index("ix_sync_dead_letters_unresolved", "resolved_at", postgresql_where=resolved_at.is_(None)),
    )
