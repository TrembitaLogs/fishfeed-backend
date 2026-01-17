"""Pydantic schemas for fish endpoints."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.species import SpeciesResponse

AddedVia = Literal["manual", "ai_scan"]


class FishCreate(BaseModel):
    """Schema for adding fish to an aquarium."""

    species_id: str = Field(min_length=1, max_length=50)
    quantity: int = Field(default=1, ge=1)
    custom_name: str | None = Field(default=None, max_length=100)
    added_via: AddedVia = Field(default="manual")


class FishUpdate(BaseModel):
    """Schema for partial fish update. All fields are optional."""

    quantity: int | None = Field(default=None, ge=1)
    custom_name: str | None = Field(default=None, max_length=100)


class FishResponse(BaseModel):
    """Response schema for fish data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    aquarium_id: UUID
    species_id: str
    species: SpeciesResponse | None = None
    quantity: int
    custom_name: str | None
    added_via: str
    created_at: datetime
