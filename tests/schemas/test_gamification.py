"""Tests for gamification Pydantic schemas."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.gamification import (
    AchievementResponse,
    AchievementType,
    ShareAchievementRequest,
    StreakResponse,
    UserStatsResponse,
)


class TestAchievementType:
    """Tests for AchievementType enum."""

    def test_all_achievement_types_defined(self):
        """Test that all expected achievement types are defined."""
        expected_types = [
            "first_feed",
            "streak_7",
            "streak_30",
            "streak_100",
            "perfect_week",
            "early_bird",
            "night_owl",
            "feeding_100",
            "feeding_500",
            "first_fish",
            "fish_collector_10",
            "fish_collector_50",
            "species_explorer_5",
            "species_explorer_20",
            "first_aquarium",
            "aquarium_collector_3",
            "aquarium_collector_10",
            "family_first",
            "family_team_3",
            "first_share",
        ]

        actual_types = [t.value for t in AchievementType]
        assert len(actual_types) == 20
        for expected in expected_types:
            assert expected in actual_types

    def test_achievement_type_is_str_enum(self):
        """Test that AchievementType values are strings."""
        assert AchievementType.FIRST_FEED == "first_feed"
        assert AchievementType.STREAK_7 == "streak_7"
        assert isinstance(AchievementType.FIRST_FEED, str)

    def test_achievement_type_from_string(self):
        """Test that AchievementType can be created from string value."""
        assert AchievementType("first_feed") == AchievementType.FIRST_FEED
        assert AchievementType("streak_30") == AchievementType.STREAK_30

    def test_invalid_achievement_type_rejected(self):
        """Test that invalid achievement type raises ValueError."""
        with pytest.raises(ValueError):
            AchievementType("invalid_type")


class TestStreakResponse:
    """Tests for StreakResponse schema."""

    def test_valid_streak_response(self):
        """Test valid StreakResponse creation."""
        response = StreakResponse(
            current_streak=7,
            best_streak=14,
            freeze_available=2,
            last_feed_date=date(2024, 1, 15),
        )

        assert response.current_streak == 7
        assert response.best_streak == 14
        assert response.freeze_available == 2
        assert response.last_feed_date == date(2024, 1, 15)

    def test_streak_response_with_null_last_feed_date(self):
        """Test StreakResponse with null last_feed_date (new user)."""
        response = StreakResponse(
            current_streak=0,
            best_streak=0,
            freeze_available=2,
            last_feed_date=None,
        )

        assert response.current_streak == 0
        assert response.last_feed_date is None

    def test_negative_streak_rejected(self):
        """Test that negative current_streak is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            StreakResponse(
                current_streak=-1,
                best_streak=0,
                freeze_available=2,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("current_streak",) for e in errors)

    def test_negative_best_streak_rejected(self):
        """Test that negative best_streak is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            StreakResponse(
                current_streak=0,
                best_streak=-5,
                freeze_available=2,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("best_streak",) for e in errors)

    def test_negative_freeze_available_rejected(self):
        """Test that negative freeze_available is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            StreakResponse(
                current_streak=0,
                best_streak=0,
                freeze_available=-1,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("freeze_available",) for e in errors)

    def test_from_orm_model(self):
        """Test StreakResponse creation from ORM model."""

        class MockStreak:
            def __init__(self):
                self.current_streak = 10
                self.best_streak = 25
                self.freeze_available = 1
                self.last_feed_date = date(2024, 1, 20)

        mock = MockStreak()
        response = StreakResponse.model_validate(mock)

        assert response.current_streak == mock.current_streak
        assert response.best_streak == mock.best_streak
        assert response.freeze_available == mock.freeze_available
        assert response.last_feed_date == mock.last_feed_date

    def test_json_serialization(self):
        """Test StreakResponse can be serialized to JSON."""
        response = StreakResponse(
            current_streak=5,
            best_streak=10,
            freeze_available=2,
            last_feed_date=date(2024, 1, 15),
        )

        data = response.model_dump()
        assert data["current_streak"] == 5
        assert data["best_streak"] == 10
        assert data["freeze_available"] == 2
        assert data["last_feed_date"] == date(2024, 1, 15)


class TestAchievementResponse:
    """Tests for AchievementResponse schema."""

    def test_valid_achievement_response(self):
        """Test valid AchievementResponse creation."""
        achievement_id = uuid4()
        unlocked_at = datetime.now(UTC)

        response = AchievementResponse(
            id=achievement_id,
            achievement_type=AchievementType.FIRST_FEED,
            unlocked_at=unlocked_at,
            shared_at=None,
        )

        assert response.id == achievement_id
        assert response.achievement_type == AchievementType.FIRST_FEED
        assert response.unlocked_at == unlocked_at
        assert response.shared_at is None

    def test_achievement_response_with_shared_at(self):
        """Test AchievementResponse with shared_at timestamp."""
        unlocked_at = datetime.now(UTC)
        shared_at = datetime.now(UTC)

        response = AchievementResponse(
            id=uuid4(),
            achievement_type=AchievementType.STREAK_7,
            unlocked_at=unlocked_at,
            shared_at=shared_at,
        )

        assert response.shared_at == shared_at

    def test_achievement_type_from_string(self):
        """Test AchievementResponse accepts string for achievement_type."""
        response = AchievementResponse(
            id=uuid4(),
            achievement_type="first_feed",
            unlocked_at=datetime.now(UTC),
        )

        assert response.achievement_type == AchievementType.FIRST_FEED

    def test_invalid_achievement_type_rejected(self):
        """Test that invalid achievement_type is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AchievementResponse(
                id=uuid4(),
                achievement_type="invalid_type",
                unlocked_at=datetime.now(UTC),
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("achievement_type",) for e in errors)

    def test_from_orm_model(self):
        """Test AchievementResponse creation from ORM model."""

        class MockAchievement:
            def __init__(self):
                self.id = uuid4()
                self.achievement_type = "streak_30"
                self.unlocked_at = datetime.now(UTC)
                self.shared_at = None

        mock = MockAchievement()
        response = AchievementResponse.model_validate(mock)

        assert response.id == mock.id
        assert response.achievement_type == AchievementType.STREAK_30
        assert response.unlocked_at == mock.unlocked_at

    def test_json_serialization(self):
        """Test AchievementResponse can be serialized to JSON."""
        response = AchievementResponse(
            id=uuid4(),
            achievement_type=AchievementType.FISH_COLLECTOR_10,
            unlocked_at=datetime.now(UTC),
            shared_at=None,
        )

        json_data = response.model_dump_json()
        assert "fish_collector_10" in json_data
        assert "T" in json_data  # ISO format contains 'T' separator


class TestUserStatsResponse:
    """Tests for UserStatsResponse schema."""

    def test_valid_user_stats_response(self):
        """Test valid UserStatsResponse creation."""
        streak = StreakResponse(
            current_streak=7,
            best_streak=14,
            freeze_available=2,
            last_feed_date=date(2024, 1, 15),
        )

        achievement = AchievementResponse(
            id=uuid4(),
            achievement_type=AchievementType.FIRST_FEED,
            unlocked_at=datetime.now(UTC),
            shared_at=None,
        )

        response = UserStatsResponse(
            streak=streak,
            achievements=[achievement],
            total_feedings=50,
            fish_count=10,
        )

        assert response.streak.current_streak == 7
        assert len(response.achievements) == 1
        assert response.total_feedings == 50
        assert response.fish_count == 10

    def test_user_stats_with_empty_achievements(self):
        """Test UserStatsResponse with no achievements."""
        streak = StreakResponse(
            current_streak=0,
            best_streak=0,
            freeze_available=2,
            last_feed_date=None,
        )

        response = UserStatsResponse(
            streak=streak,
            achievements=[],
            total_feedings=0,
            fish_count=0,
        )

        assert response.achievements == []
        assert response.total_feedings == 0

    def test_user_stats_with_multiple_achievements(self):
        """Test UserStatsResponse with multiple achievements."""
        streak = StreakResponse(
            current_streak=30,
            best_streak=30,
            freeze_available=0,
            last_feed_date=date(2024, 1, 30),
        )

        achievements = [
            AchievementResponse(
                id=uuid4(),
                achievement_type=AchievementType.FIRST_FEED,
                unlocked_at=datetime.now(UTC),
            ),
            AchievementResponse(
                id=uuid4(),
                achievement_type=AchievementType.STREAK_7,
                unlocked_at=datetime.now(UTC),
            ),
            AchievementResponse(
                id=uuid4(),
                achievement_type=AchievementType.STREAK_30,
                unlocked_at=datetime.now(UTC),
            ),
        ]

        response = UserStatsResponse(
            streak=streak,
            achievements=achievements,
            total_feedings=100,
            fish_count=15,
        )

        assert len(response.achievements) == 3

    def test_negative_total_feedings_rejected(self):
        """Test that negative total_feedings is rejected."""
        streak = StreakResponse(
            current_streak=0,
            best_streak=0,
            freeze_available=2,
        )

        with pytest.raises(ValidationError) as exc_info:
            UserStatsResponse(
                streak=streak,
                achievements=[],
                total_feedings=-1,
                fish_count=0,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("total_feedings",) for e in errors)

    def test_negative_fish_count_rejected(self):
        """Test that negative fish_count is rejected."""
        streak = StreakResponse(
            current_streak=0,
            best_streak=0,
            freeze_available=2,
        )

        with pytest.raises(ValidationError) as exc_info:
            UserStatsResponse(
                streak=streak,
                achievements=[],
                total_feedings=0,
                fish_count=-5,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("fish_count",) for e in errors)

    def test_default_achievements_list(self):
        """Test that achievements defaults to empty list."""
        streak = StreakResponse(
            current_streak=0,
            best_streak=0,
            freeze_available=2,
        )

        response = UserStatsResponse(
            streak=streak,
            total_feedings=0,
            fish_count=0,
        )

        assert response.achievements == []

    def test_json_serialization(self):
        """Test UserStatsResponse can be serialized to JSON."""
        streak = StreakResponse(
            current_streak=5,
            best_streak=10,
            freeze_available=1,
            last_feed_date=date(2024, 1, 15),
        )

        response = UserStatsResponse(
            streak=streak,
            achievements=[],
            total_feedings=25,
            fish_count=5,
        )

        data = response.model_dump()
        assert data["streak"]["current_streak"] == 5
        assert data["total_feedings"] == 25
        assert data["fish_count"] == 5


class TestShareAchievementRequest:
    """Tests for ShareAchievementRequest schema."""

    def test_empty_request(self):
        """Test ShareAchievementRequest can be created empty."""
        request = ShareAchievementRequest()
        assert request is not None

    def test_json_serialization(self):
        """Test ShareAchievementRequest can be serialized to JSON."""
        request = ShareAchievementRequest()
        data = request.model_dump()
        assert data == {}
