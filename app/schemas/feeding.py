"""Pydantic schemas for feeding schedule and feeding log endpoints."""

from datetime import date, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FeedingAction(str, Enum):
    """Possible feeding log actions."""

    fed = "fed"
    skipped = "skipped"


class ScheduleCreate(BaseModel):
    """Schema for creating a feeding schedule."""

    fish_id: UUID
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    interval_days: int = Field(ge=1, le=30)
    anchor_date: date
    food_type: str = Field(max_length=50)
    portion_hint: str | None = Field(default=None, max_length=255)

    @field_validator("time")
    @classmethod
    def validate_time_value(cls, v: str) -> str:
        """Validate that time string represents a valid HH:MM value."""
        parts = v.split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid time value: {v}. Hours must be 0-23, minutes 0-59.")
        return v


class ScheduleUpdate(BaseModel):
    """Schema for partial schedule update. All fields are optional."""

    fish_id: UUID | None = None
    time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    interval_days: int | None = Field(default=None, ge=1, le=30)
    anchor_date: date | None = None
    food_type: str | None = Field(default=None, max_length=50)
    portion_hint: str | None = Field(default=None, max_length=255)
    active: bool | None = None

    @field_validator("time")
    @classmethod
    def validate_time_value(cls, v: str | None) -> str | None:
        """Validate that time string represents a valid HH:MM value."""
        if v is None:
            return v
        parts = v.split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid time value: {v}. Hours must be 0-23, minutes 0-59.")
        return v


class ScheduleResponse(BaseModel):
    """Response schema for feeding schedule data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    aquarium_id: UUID
    fish_id: UUID
    time: str
    interval_days: int
    anchor_date: date
    active: bool
    food_type: str
    portion_hint: str | None
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime

    @field_validator("time", mode="before")
    @classmethod
    def serialize_time(cls, v: object) -> str:
        """Convert time objects to HH:MM string."""
        if hasattr(v, "strftime"):
            return v.strftime("%H:%M")  # type: ignore[no-any-return]
        return str(v)


class FeedingLogCreate(BaseModel):
    """Schema for creating a feeding log entry."""

    schedule_id: UUID
    fish_id: UUID
    scheduled_for: datetime
    action: FeedingAction
    device_id: UUID
    notes: str | None = Field(default=None, max_length=500)


class FeedingLogResponse(BaseModel):
    """Response schema for feeding log data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    schedule_id: UUID
    fish_id: UUID
    aquarium_id: UUID
    scheduled_for: datetime
    action: str
    acted_at: datetime
    acted_by_user_id: UUID
    device_id: UUID
    notes: str | None
    created_at: datetime
    acted_by_user_name: str | None = None


class FeedingLogConflictResponse(BaseModel):
    """Response schema for duplicate feeding log conflict (409)."""

    error: str
    message: str
    existing_log: FeedingLogResponse
