from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.user import JSONType

if TYPE_CHECKING:
    from app.models.fish import Fish


class Species(Base, TimestampMixin):
    """Fish species reference model with care information.

    Note: A GIN index for full-text search on (common_name, scientific_name)
    should be created in the Alembic migration as it's PostgreSQL-specific:
    CREATE INDEX idx_species_fulltext ON species
    USING GIN (to_tsvector('english', coalesce(common_name, '') || ' ' || coalesce(scientific_name, '')));
    """

    __tablename__ = "species"

    id: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
    )
    common_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    scientific_name: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    image_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    food_types: Mapped[list] = mapped_column(
        JSONType,
        default=list,
        nullable=False,
    )
    feeding_frequency: Mapped[int] = mapped_column(
        Integer,
        default=2,
        nullable=False,
    )
    portion_hint: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    care_level: Mapped[str] = mapped_column(
        String(20),
        default="beginner",
        nullable=False,
    )
    water_type: Mapped[str] = mapped_column(
        String(20),
        default="freshwater",
        nullable=False,
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONType,
        default=dict,
        nullable=False,
    )

    # Relationships
    fish: Mapped[list[Fish]] = relationship(
        "Fish",
        back_populates="species",
    )
