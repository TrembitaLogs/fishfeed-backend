"""Pydantic schemas for species endpoints."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

# Valid values for constrained fields
CareLevel = Literal["beginner", "intermediate", "advanced"]
WaterType = Literal["freshwater", "saltwater", "brackish"]
FoodType = Literal["flakes", "pellets", "frozen", "live", "vegetables", "algae"]


class SpeciesBase(BaseModel):
    """Base schema for species data."""

    common_name: str = Field(min_length=1, max_length=100)
    scientific_name: str | None = Field(default=None, max_length=150)
    food_types: list[FoodType] = Field(default_factory=list)
    feeding_frequency: int = Field(default=2, ge=1, le=10)

    @field_validator("food_types")
    @classmethod
    def validate_food_types_not_empty(cls, v: list[FoodType]) -> list[FoodType]:
        """Validate that food_types is not empty when provided."""
        if v is not None and len(v) == 0:
            raise ValueError("food_types cannot be an empty list")
        return v


class SpeciesCreate(SpeciesBase):
    """Schema for creating a new species."""

    id: str = Field(min_length=1, max_length=50)
    image_url: str | None = Field(default=None)
    portion_hint: str | None = Field(default=None, max_length=255)
    care_level: CareLevel = Field(default="beginner")
    water_type: WaterType = Field(default="freshwater")
    metadata: dict | None = Field(default=None)


class SpeciesUpdate(BaseModel):
    """Schema for partial species update. All fields are optional."""

    common_name: str | None = Field(default=None, min_length=1, max_length=100)
    scientific_name: str | None = Field(default=None, max_length=150)
    food_types: list[FoodType] | None = Field(default=None)
    feeding_frequency: int | None = Field(default=None, ge=1, le=10)
    image_url: str | None = Field(default=None)
    portion_hint: str | None = Field(default=None, max_length=255)
    care_level: CareLevel | None = Field(default=None)
    water_type: WaterType | None = Field(default=None)
    metadata: dict | None = Field(default=None)

    @field_validator("food_types")
    @classmethod
    def validate_food_types_not_empty(
        cls, v: list[FoodType] | None
    ) -> list[FoodType] | None:
        """Validate that food_types is not empty when provided."""
        if v is not None and len(v) == 0:
            raise ValueError("food_types cannot be an empty list")
        return v


class SpeciesResponse(BaseModel):
    """Response schema for species data."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    common_name: str
    scientific_name: str | None
    image_url: str | None
    food_types: list[str]
    feeding_frequency: int
    portion_hint: str | None
    care_level: str
    water_type: str
    metadata: Annotated[dict | None, Field(alias="metadata_")] = None
    created_at: datetime
    updated_at: datetime


class SpeciesListResponse(BaseModel):
    """Paginated response for species list."""

    items: list[SpeciesResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    per_page: int = Field(ge=1, le=100)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pages(self) -> int:
        """Calculate total number of pages."""
        if self.total == 0:
            return 0
        return (self.total + self.per_page - 1) // self.per_page


class SpeciesSearchQuery(BaseModel):
    """Query parameters for species search."""

    q: str | None = Field(default=None, min_length=1, max_length=100)
    care_level: CareLevel | None = Field(default=None)
    water_type: WaterType | None = Field(default=None)
