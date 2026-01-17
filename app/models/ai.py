import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.user import JSONType

if TYPE_CHECKING:
    from app.models.species import Species
    from app.models.user import User


class AIScan(Base):
    """AI fish species scan log.

    Tracks AI-powered fish identification attempts with confidence scores
    and user corrections for model improvement.
    """

    __tablename__ = "ai_scans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    image_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    image_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    detected_species_id: Mapped[str | None] = mapped_column(
        String(50),
        ForeignKey("species.id", ondelete="SET NULL"),
        nullable=True,
    )
    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    alternatives: Mapped[list | None] = mapped_column(
        JSONType,
        nullable=True,
    )
    confirmed_species_id: Mapped[str | None] = mapped_column(
        String(50),
        ForeignKey("species.id", ondelete="SET NULL"),
        nullable=True,
    )
    was_corrected: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    processing_time_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    user: Mapped[User] = relationship(
        "User",
        back_populates="ai_scans",
    )
    detected_species: Mapped[Species | None] = relationship(
        "Species",
        foreign_keys=[detected_species_id],
    )
    confirmed_species: Mapped[Species | None] = relationship(
        "Species",
        foreign_keys=[confirmed_species_id],
    )
