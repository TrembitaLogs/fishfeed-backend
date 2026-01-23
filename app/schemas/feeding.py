"""Pydantic schemas for feeding schedule and events endpoints."""

from datetime import datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FeedingStatus = Literal["pending", "completed", "missed"]


class ScheduleCreate(BaseModel):
    """Schema for creating a feeding schedule."""

    times_per_day: int = Field(ge=1, le=10)
    scheduled_times: list[time] = Field(min_length=1, max_length=10)
    food_type: str | None = Field(default=None, max_length=50)
    portion_hint: str | None = Field(default=None, max_length=255)

    @field_validator("scheduled_times")
    @classmethod
    def sort_and_deduplicate_times(cls, v: list[time]) -> list[time]:
        """Sort times and remove duplicates."""
        unique_times = list(set(v))
        return sorted(unique_times)

    @model_validator(mode="after")
    def validate_times_count(self) -> ScheduleCreate:
        """Validate that times_per_day matches the number of scheduled_times."""
        if self.times_per_day != len(self.scheduled_times):
            raise ValueError(
                f"times_per_day ({self.times_per_day}) must equal "
                f"the number of scheduled_times ({len(self.scheduled_times)})"
            )
        return self


class ScheduleUpdate(BaseModel):
    """Schema for partial schedule update. All fields are optional."""

    times_per_day: int | None = Field(default=None, ge=1, le=10)
    scheduled_times: list[time] | None = Field(default=None, min_length=1, max_length=10)
    food_type: str | None = Field(default=None, max_length=50)
    portion_hint: str | None = Field(default=None, max_length=255)

    @field_validator("scheduled_times")
    @classmethod
    def sort_and_deduplicate_times(cls, v: list[time] | None) -> list[time] | None:
        """Sort times and remove duplicates if provided."""
        if v is None:
            return v
        unique_times = list(set(v))
        return sorted(unique_times)

    @model_validator(mode="after")
    def validate_times_count(self) -> ScheduleUpdate:
        """Validate that times_per_day matches the number of scheduled_times when both provided."""
        if self.times_per_day is not None and self.scheduled_times is not None:
            if self.times_per_day != len(self.scheduled_times):
                raise ValueError(
                    f"times_per_day ({self.times_per_day}) must equal "
                    f"the number of scheduled_times ({len(self.scheduled_times)})"
                )
        return self


class ScheduleResponse(BaseModel):
    """Response schema for feeding schedule data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    aquarium_id: UUID
    times_per_day: int
    scheduled_times: list[str]  # Stored as JSON strings in DB
    food_type: str
    portion_hint: str | None
    created_at: datetime
    updated_at: datetime


class EventResponse(BaseModel):
    """Response schema for feeding event data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    aquarium_id: UUID
    schedule_id: UUID | None
    fish_id: UUID | None = None
    species_id: str | None = None
    scheduled_at: datetime
    status: FeedingStatus
    completed_at: datetime | None
    completed_by: UUID | None


class TodayEventsResponse(BaseModel):
    """Response schema for today's feeding events."""

    events: list[EventResponse] = Field(default_factory=list)
    next_feeding: datetime | None = None
