"""Pydantic schemas for AI fish recognition endpoints."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScanRequest(BaseModel):
    """Schema for AI scan request with base64 encoded image."""

    image_base64: str | None = Field(
        default=None,
        description="Base64 encoded image data",
    )


class AlternativeSpecies(BaseModel):
    """Schema for alternative species suggestion from AI scan."""

    model_config = ConfigDict(from_attributes=True)

    species_id: str = Field(description="Fish species identifier")
    species_name: str = Field(description="Species common name")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence level between 0.0 and 1.0",
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence_range(cls, v: float) -> float:
        """Validate that confidence is within valid range."""
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v


class ScanResponse(BaseModel):
    """Response schema for AI scan results."""

    model_config = ConfigDict(from_attributes=True)

    scan_id: UUID = Field(description="Unique scan identifier")
    species_id: str | None = Field(
        default=None,
        description="Primary species match identifier",
    )
    species_name: str | None = Field(
        default=None,
        description="Primary species match name",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence level of primary match",
    )
    alternatives: list[AlternativeSpecies] = Field(
        default_factory=list,
        description="Top alternative species matches",
    )
    scans_remaining: int = Field(
        ge=0,
        description="Remaining free scans for user",
    )
    image_url: str | None = Field(
        default=None,
        description="URL of stored scan image",
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence_range(cls, v: float) -> float:
        """Validate that confidence is within valid range."""
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v


class ScanConfirmRequest(BaseModel):
    """Schema for confirming species identification."""

    species_id: str = Field(description="User-confirmed species identifier")


class ScansRemainingResponse(BaseModel):
    """Response schema for remaining scans check."""

    scans_remaining: int = Field(ge=0, description="Number of remaining free scans")
    is_premium: bool = Field(description="Whether user has premium subscription")
