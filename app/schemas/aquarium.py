"""Pydantic schemas for aquarium endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from app.schemas.feeding import ScheduleResponse
    from app.schemas.fish import FishResponse


class AquariumBase(BaseModel):
    """Base schema for aquarium data."""

    name: str = Field(min_length=1, max_length=100)


class AquariumCreate(AquariumBase):
    """Schema for creating a new aquarium."""

    pass


class AquariumUpdate(BaseModel):
    """Schema for partial aquarium update. All fields are optional."""

    name: str | None = Field(default=None, min_length=1, max_length=100)


class AquariumResponse(BaseModel):
    """Response schema for aquarium data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    owner_id: UUID
    created_at: datetime
    updated_at: datetime


class AquariumWithFish(AquariumResponse):
    """Response schema for aquarium with fish and schedules."""

    fish: list[FishResponse] = Field(default_factory=list)
    schedules: list[ScheduleResponse] = Field(default_factory=list)
