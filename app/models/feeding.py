import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin
from app.models.user import JSONType

if TYPE_CHECKING:
    from app.models.aquarium import Aquarium
    from app.models.fish import Fish
    from app.models.user import User


class FeedingSchedule(Base, TimestampMixin):
    """Feeding schedule model for aquarium feeding times."""

    __tablename__ = "feeding_schedules"

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
    times_per_day: Mapped[int] = mapped_column(
        Integer,
        default=2,
        nullable=False,
    )
    scheduled_times: Mapped[list] = mapped_column(
        JSONType,
        default=list,
        nullable=False,
    )
    food_type: Mapped[str] = mapped_column(
        String(50),
        default="flakes",
        nullable=False,
    )
    portion_hint: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # Relationships
    aquarium: Mapped[Aquarium] = relationship(
        "Aquarium",
        back_populates="feeding_schedules",
    )
    feeding_events: Mapped[list[FeedingEvent]] = relationship(
        "FeedingEvent",
        back_populates="schedule",
        cascade="all, delete-orphan",
    )


class FeedingEvent(Base, TimestampMixin, SoftDeleteMixin):
    """Feeding event model for tracking individual feeding occurrences."""

    __tablename__ = "feeding_events"

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
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feeding_schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    fish_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fish.id", ondelete="SET NULL"),
        nullable=True,
    )
    species_id: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    client_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    concurrent_with: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feeding_events.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    aquarium: Mapped[Aquarium] = relationship(
        "Aquarium",
        back_populates="feeding_events",
    )
    schedule: Mapped[FeedingSchedule | None] = relationship(
        "FeedingSchedule",
        back_populates="feeding_events",
    )
    fish: Mapped[Fish | None] = relationship(
        "Fish",
        foreign_keys=[fish_id],
    )
    completed_by_user: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[completed_by],
    )
    concurrent_event: Mapped[FeedingEvent | None] = relationship(
        "FeedingEvent",
        foreign_keys=[concurrent_with],
        remote_side="FeedingEvent.id",
    )

    __table_args__ = (
        Index(
            "idx_feeding_events_aquarium_scheduled",
            "aquarium_id",
            "scheduled_at",
        ),
        Index(
            "idx_feeding_events_pending",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "idx_feeding_events_active",
            "aquarium_id",
            "scheduled_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
