import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.feeding import FeedingLog, FeedingSchedule
    from app.models.fish import Fish
    from app.models.user import User


class Aquarium(Base, TimestampMixin, SoftDeleteMixin):
    """Aquarium model representing a user's fish tank."""

    __tablename__ = "aquariums"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    photo_key: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    water_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        server_default="freshwater",
    )
    capacity: Mapped[float | None] = mapped_column(
        Numeric(8, 2),
        nullable=True,
    )

    # Relationships
    owner: Mapped[User] = relationship(
        "User",
        back_populates="owned_aquariums",
        foreign_keys=[owner_id],
    )
    members: Mapped[list[AquariumMember]] = relationship(
        "AquariumMember",
        back_populates="aquarium",
        cascade="all, delete-orphan",
    )
    family_invites: Mapped[list[FamilyInvite]] = relationship(
        "FamilyInvite",
        back_populates="aquarium",
        cascade="all, delete-orphan",
    )
    fish: Mapped[list[Fish]] = relationship(
        "Fish",
        back_populates="aquarium",
        cascade="all, delete-orphan",
    )
    feeding_schedules: Mapped[list[FeedingSchedule]] = relationship(
        "FeedingSchedule",
        back_populates="aquarium",
        cascade="all, delete-orphan",
    )
    feeding_logs: Mapped[list[FeedingLog]] = relationship(
        "FeedingLog",
        back_populates="aquarium",
        cascade="all, delete-orphan",
    )


class AquariumMember(Base):
    """Association table for aquarium members (Family Mode)."""

    __tablename__ = "aquarium_members"

    aquarium_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aquariums.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(
        String(20),
        default="member",
        nullable=False,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    aquarium: Mapped[Aquarium] = relationship(
        "Aquarium",
        back_populates="members",
    )
    user: Mapped[User] = relationship(
        "User",
        back_populates="aquarium_memberships",
    )


class FamilyInvite(Base):
    """Family invite model for sharing aquarium access."""

    __tablename__ = "family_invites"

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
    invite_code: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        index=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    aquarium: Mapped[Aquarium] = relationship(
        "Aquarium",
        back_populates="family_invites",
    )
    creator: Mapped[User] = relationship(
        "User",
        foreign_keys=[created_by],
    )
    used_by_user: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[used_by],
    )
