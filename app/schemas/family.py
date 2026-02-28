"""Pydantic schemas for Family Mode endpoints."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FamilyMemberResponse(BaseModel):
    """Response schema for a family member."""

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    nickname: str | None = None
    avatar_key: str | None = None
    role: Literal["owner", "member"]
    joined_at: datetime


class InviteResponse(BaseModel):
    """Response schema for invite data."""

    invite_code: str = Field(min_length=8, max_length=8)
    invite_link: str
    expires_at: datetime


class CreateInviteResponse(InviteResponse):
    """Response schema for creating a new invite."""

    aquarium_id: UUID


class AcceptInviteRequest(BaseModel):
    """Request schema for accepting an invite."""

    invite_code: str = Field(min_length=1, max_length=32)

    @field_validator("invite_code")
    @classmethod
    def validate_invite_code_format(cls, v: str) -> str:
        """Validate invite code contains only URL-safe characters (alphanumeric, -, _)."""
        if not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(
                "Invite code must contain only URL-safe characters (letters, digits, -, _)"
            )
        return v


class FamilyListResponse(BaseModel):
    """Response schema for family members list."""

    aquarium_id: UUID
    members: list[FamilyMemberResponse] = Field(default_factory=list)
