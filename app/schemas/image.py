"""Pydantic schemas for image upload and presigned URL endpoints."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

EntityType = Literal["aquarium", "fish", "avatar"]


class UploadResponse(BaseModel):
    """Response schema for successful image upload (201 Created)."""

    key: str = Field(description="S3 object key for the uploaded image")
    entity_type: str = Field(description="Entity type: aquarium, fish, or avatar")
    entity_id: str = Field(description="UUID of the entity")


class PresignedUrlItem(BaseModel):
    """Single item in a batch presigned URL request."""

    entity_type: EntityType = Field(description="Entity type: aquarium, fish, or avatar")
    entity_id: uuid.UUID = Field(description="UUID of the entity")


class PresignedUrlsRequest(BaseModel):
    """Request schema for batch presigned URL generation."""

    items: list[PresignedUrlItem] = Field(
        description="List of entities to get presigned URLs for",
    )


class PresignedUrlResult(BaseModel):
    """Single item in a batch presigned URL response."""

    entity_type: str = Field(description="Entity type: aquarium, fish, or avatar")
    entity_id: uuid.UUID = Field(description="UUID of the entity")
    key: str | None = Field(description="S3 object key, null if entity has no photo")
    url: str | None = Field(description="Presigned GET URL, null if entity has no photo")


class PresignedUrlsResponse(BaseModel):
    """Response schema for batch presigned URL generation."""

    items: list[PresignedUrlResult] = Field(
        description="Presigned URLs for accessible entities",
    )
