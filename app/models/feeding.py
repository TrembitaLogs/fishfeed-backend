import uuid
from datetime import date, datetime
from datetime import time as dt_time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.aquarium import Aquarium
    from app.models.fish import Fish
    from app.models.user import User


class FeedingSchedule(Base, TimestampMixin):
    """Feeding schedule model — one rule per fish per time slot."""

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
    fish_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fish.id", ondelete="CASCADE"),
        nullable=False,
    )
    time: Mapped[dt_time] = mapped_column(
        Time,
        nullable=False,
        server_default=text("'09:00'"),
    )
    interval_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    anchor_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=text("CURRENT_DATE"),
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
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )

    # Relationships
    aquarium: Mapped[Aquarium] = relationship(
        "Aquarium",
        back_populates="feeding_schedules",
    )
    fish: Mapped[Fish] = relationship(
        "Fish",
        foreign_keys=[fish_id],
    )
    created_by_user: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[created_by_user_id],
    )
    feeding_logs: Mapped[list[FeedingLog]] = relationship(
        "FeedingLog",
        back_populates="schedule",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("fish_id", "time", name="uq_feeding_schedules_fish_time"),
        Index("idx_feeding_schedules_fish_id", "fish_id"),
        Index("idx_feeding_schedules_aquarium_id", "aquarium_id"),
        Index(
            "idx_feeding_schedules_active",
            "active",
            postgresql_where=text("active = true"),
        ),
        Index(
            "idx_feeding_schedules_active_aquarium",
            "active",
            "aquarium_id",
        ),
    )


class FeedingLog(Base):
    """Feeding log model — records the fact that a feeding happened or was skipped."""

    __tablename__ = "feeding_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feeding_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    fish_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fish.id", ondelete="CASCADE"),
        nullable=False,
    )
    aquarium_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aquariums.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    acted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
    acted_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )

    # Relationships
    schedule: Mapped[FeedingSchedule] = relationship(
        "FeedingSchedule",
        back_populates="feeding_logs",
    )
    fish: Mapped[Fish] = relationship(
        "Fish",
        foreign_keys=[fish_id],
    )
    aquarium: Mapped[Aquarium] = relationship(
        "Aquarium",
        back_populates="feeding_logs",
    )
    acted_by_user: Mapped[User] = relationship(
        "User",
        foreign_keys=[acted_by_user_id],
    )

    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_for", name="uq_feeding_logs_schedule_scheduled_for"),
        Index("idx_feeding_logs_aquarium_id", "aquarium_id"),
        Index("idx_feeding_logs_fish_id", "fish_id"),
        Index("idx_feeding_logs_scheduled_for", "scheduled_for"),
        Index("idx_feeding_logs_acted_at", "acted_at"),
        # Supports the delta-sync filter (aquarium_id + created_at >= since) and the
        # paginated ORDER BY (created_at, id) used by get_paginated_server_state.
        Index("idx_feeding_logs_aquarium_created", "aquarium_id", "created_at"),
    )
