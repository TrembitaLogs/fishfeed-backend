"""Pydantic schemas for gamification endpoints (streaks, achievements, stats)."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AchievementType(StrEnum):
    """Available achievement types for gamification.

    Categories:
    - Feeding: first_feed, streak_*, perfect_week, early_bird, night_owl, feeding_*
    - Fish: first_fish, fish_collector_*, species_explorer_*
    - Aquarium: first_aquarium, aquarium_collector_*
    - Family: family_first, family_team_*
    - Social: first_share
    """

    # Feeding achievements
    FIRST_FEED = "first_feed"
    STREAK_7 = "streak_7"
    STREAK_30 = "streak_30"
    STREAK_100 = "streak_100"
    PERFECT_WEEK = "perfect_week"
    EARLY_BIRD = "early_bird"
    NIGHT_OWL = "night_owl"
    FEEDING_100 = "feeding_100"
    FEEDING_500 = "feeding_500"

    # Fish achievements
    FIRST_FISH = "first_fish"
    FISH_COLLECTOR_10 = "fish_collector_10"
    FISH_COLLECTOR_50 = "fish_collector_50"
    SPECIES_EXPLORER_5 = "species_explorer_5"
    SPECIES_EXPLORER_20 = "species_explorer_20"

    # Aquarium achievements
    FIRST_AQUARIUM = "first_aquarium"
    AQUARIUM_COLLECTOR_3 = "aquarium_collector_3"
    AQUARIUM_COLLECTOR_10 = "aquarium_collector_10"

    # Family achievements
    FAMILY_FIRST = "family_first"
    FAMILY_TEAM_3 = "family_team_3"

    # Social achievements
    FIRST_SHARE = "first_share"


class StreakResponse(BaseModel):
    """Response schema for user streak data."""

    model_config = ConfigDict(from_attributes=True)

    current_streak: int = Field(ge=0, description="Current consecutive days streak")
    best_streak: int = Field(ge=0, description="Best streak ever achieved")
    freeze_available: int = Field(ge=0, description="Number of freeze days available")
    last_feed_date: date | None = Field(
        default=None, description="Date of the last feeding"
    )


class AchievementResponse(BaseModel):
    """Response schema for a single achievement."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    achievement_type: AchievementType
    unlocked_at: datetime
    shared_at: datetime | None = None


class UserStatsResponse(BaseModel):
    """Response schema for aggregated user gamification stats."""

    streak: StreakResponse
    achievements: list[AchievementResponse] = Field(default_factory=list)
    total_feedings: int = Field(ge=0, description="Total number of feedings completed")
    fish_count: int = Field(ge=0, description="Total number of fish owned")


class ShareAchievementRequest(BaseModel):
    """Request schema for sharing an achievement."""

    pass
