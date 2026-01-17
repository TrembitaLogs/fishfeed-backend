"""Analytics event model for product analytics storage."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.user import JSONType


class AnalyticsEvent(Base):
    """Analytics event model for tracking user behavior.

    Stores product analytics events with optional external forwarding
    to PostHog/Amplitude. Supports anonymization for GDPR compliance.
    """

    __tablename__ = "analytics_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    properties: Mapped[dict] = mapped_column(
        JSONType,
        default=dict,
        nullable=False,
    )
    device_info: Mapped[dict | None] = mapped_column(
        JSONType,
        nullable=True,
    )
    ip_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    anonymized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "idx_analytics_events_user_created",
            "user_id",
            "created_at",
        ),
        Index(
            "idx_analytics_events_type_created",
            "event_type",
            "created_at",
        ),
    )
