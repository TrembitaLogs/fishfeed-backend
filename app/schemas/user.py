"""Pydantic schemas for user profile endpoints."""

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.gamification import StreakResponse


class UserProfileResponse(BaseModel):
    """Response schema for user profile data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None = Field(
        default=None, description="User display name (nickname)"
    )
    avatar_url: str | None = Field(default=None, description="URL to user avatar image")
    created_at: datetime
    subscription_status: str = Field(
        default="free", description="Subscription status (free/premium)"
    )
    subscription_expires_at: datetime | None = Field(
        default=None, description="When premium subscription expires"
    )
    streak: StreakResponse | None = Field(
        default=None, description="User's current streak data"
    )
    achievements_count: int = Field(
        default=0, ge=0, description="Number of unlocked achievements"
    )


class UserProfileUpdateRequest(BaseModel):
    """Request schema for updating user profile."""

    display_name: str | None = Field(
        default=None, max_length=50, description="User display name"
    )
    avatar_url: str | None = Field(
        default=None, max_length=2048, description="URL to user avatar image"
    )

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, v: str | None) -> str | None:
        """Validate that avatar_url is a valid HTTP/HTTPS URL."""
        if v is None:
            return v

        if v == "":
            return None

        url_pattern = re.compile(
            r"^https?://"  # http:// or https://
            r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"  # domain
            r"localhost|"  # localhost
            r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # or IP
            r"(?::\d+)?"  # optional port
            r"(?:/?|[/?]\S+)$",
            re.IGNORECASE,
        )

        if not url_pattern.match(v):
            raise ValueError("avatar_url must be a valid HTTP or HTTPS URL")

        return v
