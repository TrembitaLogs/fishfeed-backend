import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin

# Use JSONB for PostgreSQL, fallback to JSON for other databases (like SQLite in tests)
JSONType = JSON().with_variant(JSONB(), "postgresql")

if TYPE_CHECKING:
    from app.models.ai import AIScan
    from app.models.aquarium import Aquarium, AquariumMember
    from app.models.gamification import Achievement, Streak, UserProgress
    from app.models.notification import NotificationPreference, PushToken


class User(Base, TimestampMixin, SoftDeleteMixin):
    """User model for authentication and profile data."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    password_hash: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    oauth_provider: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    oauth_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    nickname: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    avatar_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    email_verified: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
    )
    subscription_status: Mapped[str] = mapped_column(
        String(20),
        default="free",
        nullable=False,
    )
    subscription_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    free_ai_scans_remaining: Mapped[int] = mapped_column(
        default=5,
        nullable=False,
    )
    settings: Mapped[dict] = mapped_column(
        JSONType,
        default=dict,
        nullable=False,
    )
    is_admin: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
    )
    token_version: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
        server_default="0",
    )

    # Relationships
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    owned_aquariums: Mapped[list[Aquarium]] = relationship(
        "Aquarium",
        back_populates="owner",
        foreign_keys="Aquarium.owner_id",
    )
    aquarium_memberships: Mapped[list[AquariumMember]] = relationship(
        "AquariumMember",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    streak: Mapped[Streak | None] = relationship(
        "Streak",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    achievements: Mapped[list[Achievement]] = relationship(
        "Achievement",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    ai_scans: Mapped[list[AIScan]] = relationship(
        "AIScan",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    push_tokens: Mapped[list[PushToken]] = relationship(
        "PushToken",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    notification_preferences: Mapped[NotificationPreference | None] = relationship(
        "NotificationPreference",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    progress: Mapped[UserProgress | None] = relationship(
        "UserProgress",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )


class RefreshToken(Base):
    """Refresh token model for JWT authentication."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    device_info: Mapped[dict | None] = mapped_column(
        JSONType,
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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
        back_populates="refresh_tokens",
    )

    __table_args__ = (
        Index(
            "idx_refresh_tokens_user_active",
            "user_id",
            postgresql_where=(revoked_at.is_(None)),
        ),
    )
