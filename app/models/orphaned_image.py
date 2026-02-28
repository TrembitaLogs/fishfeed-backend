import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrphanedImage(Base):
    """Tracks S3 image keys that are no longer referenced by any entity.

    When a photo is replaced or removed, the old S3 key is stored here
    with a grace period before actual deletion from S3.
    """

    __tablename__ = "orphaned_images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    old_key: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    orphaned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_orphaned_images_orphaned_at", "orphaned_at"),
    )
