"""Pydantic schemas for user profile endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.gamification import StreakResponse


class UserProfileResponse(BaseModel):
    """Response schema for user profile data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None = Field(
        default=None, description="User display name (nickname)"
    )
    avatar_key: str | None = Field(default=None, description="S3 object key for user avatar image")
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
    avatar_key: str | None = Field(
        default=None, max_length=500, description="S3 object key for user avatar image"
    )
