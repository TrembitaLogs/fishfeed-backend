"""Pydantic schemas for analytics events and GDPR data export."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, HttpUrl


class EventRequest(BaseModel):
    """Schema for a single analytics event."""

    event_type: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Event type identifier (snake_case, e.g., 'button_click', 'page_view')",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Flexible event properties stored as JSONB",
    )
    timestamp: datetime | None = Field(
        default=None,
        description="Event timestamp. If None, server time will be used.",
    )
    device_info: dict[str, str] | None = Field(
        default=None,
        description="Optional device metadata (os, version, device_model, etc.)",
    )


class EventBatchRequest(BaseModel):
    """Schema for batch analytics events submission."""

    events: list[EventRequest] = Field(
        min_length=1,
        max_length=100,
        description="List of events to track (1-100 events per batch)",
    )

    @field_validator("events")
    @classmethod
    def validate_batch_size(cls, v: list[EventRequest]) -> list[EventRequest]:
        """Validate batch size is within limits."""
        if len(v) > 100:
            raise ValueError("Batch size cannot exceed 100 events")
        return v


EventStatus = Literal["accepted", "queued"]


class EventResponse(BaseModel):
    """Response schema for event tracking endpoints."""

    status: EventStatus = Field(description="Event processing status")
    event_count: int = Field(ge=1, description="Number of events processed")


class DataExportResponse(BaseModel):
    """Response schema for GDPR data export."""

    model_config = ConfigDict(from_attributes=True)

    download_url: HttpUrl = Field(description="Presigned URL to download exported data")
    expires_at: datetime = Field(description="URL expiration timestamp")
    file_size_bytes: int = Field(ge=0, description="Size of the export file in bytes")
    format: Literal["json"] = Field(default="json", description="Export file format")


class RateLimitInfo(BaseModel):
    """Schema for rate limit information in response headers."""

    limit: int = Field(ge=1, description="Maximum requests allowed in the window")
    remaining: int = Field(ge=0, description="Remaining requests in current window")
    reset_at: datetime = Field(description="When the rate limit window resets")
