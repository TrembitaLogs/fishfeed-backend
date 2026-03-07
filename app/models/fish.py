import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.aquarium import Aquarium
    from app.models.species import Species


class Fish(Base, TimestampMixin, SoftDeleteMixin):
    """Fish instance model representing fish in a user's aquarium."""

    __tablename__ = "fish"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    aquarium_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aquariums.id", ondelete="CASCADE"),
        nullable=False,
    )
    species_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("species.id"),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )
    custom_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    added_via: Mapped[str] = mapped_column(
        String(20),
        default="manual",
        nullable=False,
    )
    photo_key: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    # Relationships
    aquarium: Mapped[Aquarium] = relationship(
        "Aquarium",
        back_populates="fish",
    )
    species: Mapped[Species] = relationship(
        "Species",
        back_populates="fish",
    )

    __table_args__ = (
        Index(
            "idx_fish_aquarium_active",
            "aquarium_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
