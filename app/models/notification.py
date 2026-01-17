import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class PushToken(Base):
    """Push notification token storage.

    Stores device tokens for push notifications with platform info.
    """

    __tablename__ = "push_tokens"

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
    token: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user: Mapped[User] = relationship(
        "User",
        back_populates="push_tokens",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "token", name="uq_user_push_token"),
    )


class NotificationPreference(Base):
    """User notification preferences.

    Uses user_id as primary key (one-to-one relationship with users).
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Global opt-out: disables ALL notifications regardless of per-type settings
    global_opt_out: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    # User timezone for quiet hours calculation (e.g., "+02:00", "-05:00")
    timezone: Mapped[str | None] = mapped_column(
        String(10),
        default=None,
        nullable=True,
    )
    feeding_reminders: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    overdue_alerts: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    streak_protection: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    weekly_summary: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    family_updates: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    marketing: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user: Mapped[User] = relationship(
        "User",
        back_populates="notification_preferences",
    )


class NotificationLog(Base):
    """Log of all sent push notifications.

    Tracks notification delivery attempts for analytics and debugging.
    """

    __tablename__ = "notification_logs"

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
    notification_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    platform: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    success: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationships
    user: Mapped[User] = relationship("User")
