"""Notification schemas for push tokens and preferences."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PlatformType = Literal["ios", "android"]


class PushTokenRequest(BaseModel):
    """Request schema for registering a push notification token."""

    token: str = Field(min_length=1, max_length=512)
    platform: PlatformType


class PushTokenResponse(BaseModel):
    """Response schema for push token."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    token: str
    platform: str
    created_at: datetime


class NotificationPreferencesUpdate(BaseModel):
    """Request schema for updating notification preferences.

    All fields are optional - only provided fields will be updated.
    """

    global_opt_out: bool | None = None
    timezone: str | None = Field(
        default=None,
        pattern=r"^[+-]\d{2}:\d{2}$",
        description="Timezone offset (e.g., '+02:00', '-05:00')",
    )
    feeding_reminders: bool | None = None
    overdue_alerts: bool | None = None
    streak_protection: bool | None = None
    weekly_summary: bool | None = None
    family_updates: bool | None = None
    marketing: bool | None = None


class NotificationPreferencesResponse(BaseModel):
    """Response schema for notification preferences."""

    model_config = ConfigDict(from_attributes=True)

    global_opt_out: bool
    timezone: str | None = None
    feeding_reminders: bool
    overdue_alerts: bool
    streak_protection: bool
    weekly_summary: bool
    family_updates: bool
    marketing: bool
    updated_at: datetime | None = None
