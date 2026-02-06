"""Pydantic schemas for admin dashboard and management endpoints."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class DashboardUsersStats(BaseModel):
    """Aggregated user statistics for the admin dashboard."""

    total: int = Field(ge=0, description="Total registered users (not soft-deleted)")
    active_last_7d: int = Field(ge=0, description="Users with feeding activity in the last 7 days")
    premium: int = Field(ge=0, description="Users with premium subscription status")
    new_today: int = Field(ge=0, description="Users registered today")


class DashboardAquariumsStats(BaseModel):
    """Aggregated aquarium statistics for the admin dashboard."""

    total: int = Field(ge=0, description="Total aquariums (not soft-deleted)")
    with_family_members: int = Field(
        ge=0, description="Aquariums that have at least one family member"
    )


class DashboardFeedingStats(BaseModel):
    """Aggregated feeding statistics for the admin dashboard."""

    logs_today: int = Field(ge=0, description="Feeding logs created today")
    schedules_active: int = Field(ge=0, description="Currently active feeding schedules")


class DashboardAIStats(BaseModel):
    """Aggregated AI scan statistics for the admin dashboard."""

    total: int = Field(ge=0, description="Total AI scans ever performed")
    today: int = Field(ge=0, description="AI scans performed today")


class DashboardGamificationStats(BaseModel):
    """Aggregated gamification statistics for the admin dashboard."""

    avg_streak: float = Field(ge=0, description="Average current streak across all users")
    max_streak: int = Field(ge=0, description="Highest current streak among all users")
    achievements_unlocked_today: int = Field(
        ge=0, description="Achievements unlocked today"
    )


class DashboardResponse(BaseModel):
    """Complete admin dashboard response combining all stat categories."""

    users: DashboardUsersStats
    aquariums: DashboardAquariumsStats
    feeding: DashboardFeedingStats
    ai_scans: DashboardAIStats
    gamification: DashboardGamificationStats


class GrantPremiumRequest(BaseModel):
    """Request body for granting premium subscription to a user."""

    days: int = Field(ge=1, description="Number of days to grant premium subscription")


class UserActionResponse(BaseModel):
    """Response for admin user management actions."""

    user_id: UUID
    action: str
    success: bool
    message: str


class UpdateSubscriptionRequest(BaseModel):
    """Request body for updating a user's subscription status."""

    status: Literal["free", "premium", "expired"] = Field(
        description="New subscription status"
    )
    expires_at: datetime | None = Field(
        default=None, description="Subscription expiration datetime (null to clear)"
    )


class SubscriptionResponse(BaseModel):
    """Response for subscription update showing current subscription state."""

    user_id: UUID
    subscription_status: str
    subscription_expires_at: datetime | None
