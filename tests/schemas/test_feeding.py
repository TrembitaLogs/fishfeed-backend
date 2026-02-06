"""Tests for feeding Pydantic schemas (Schedule + FeedingLog architecture)."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.feeding import (
    FeedingAction,
    FeedingLogCreate,
    FeedingLogResponse,
    ScheduleCreate,
    ScheduleResponse,
    ScheduleUpdate,
)


class TestScheduleCreate:
    """Tests for ScheduleCreate schema."""

    def test_valid_schedule_create(self):
        """Test valid ScheduleCreate with all fields."""
        fish_id = uuid4()
        schedule = ScheduleCreate(
            fish_id=fish_id,
            time="09:00",
            interval_days=1,
            anchor_date=date.today(),
            food_type="pellets",
            portion_hint="2-3 pellets per fish",
        )
        assert schedule.fish_id == fish_id
        assert schedule.time == "09:00"
        assert schedule.interval_days == 1
        assert schedule.food_type == "pellets"
        assert schedule.portion_hint == "2-3 pellets per fish"

    def test_minimal_schedule_create(self):
        """Test ScheduleCreate with minimal required fields."""
        schedule = ScheduleCreate(
            fish_id=uuid4(),
            time="12:00",
            interval_days=1,
            anchor_date=date.today(),
            food_type="flakes",
        )
        assert schedule.portion_hint is None

    def test_invalid_time_format_rejected(self):
        """Test that invalid time format is rejected."""
        with pytest.raises(ValidationError):
            ScheduleCreate(
                fish_id=uuid4(),
                time="9:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )

    def test_invalid_time_value_rejected(self):
        """Test that invalid time value (e.g., 25:00) is rejected."""
        with pytest.raises(ValidationError):
            ScheduleCreate(
                fish_id=uuid4(),
                time="25:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
            )

    def test_interval_days_zero_rejected(self):
        """Test that interval_days of 0 is rejected."""
        with pytest.raises(ValidationError):
            ScheduleCreate(
                fish_id=uuid4(),
                time="09:00",
                interval_days=0,
                anchor_date=date.today(),
                food_type="flakes",
            )

    def test_interval_days_above_30_rejected(self):
        """Test that interval_days above 30 is rejected."""
        with pytest.raises(ValidationError):
            ScheduleCreate(
                fish_id=uuid4(),
                time="09:00",
                interval_days=31,
                anchor_date=date.today(),
                food_type="flakes",
            )

    def test_food_type_too_long_rejected(self):
        """Test that food_type exceeding 50 chars is rejected."""
        with pytest.raises(ValidationError):
            ScheduleCreate(
                fish_id=uuid4(),
                time="12:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="A" * 51,
            )

    def test_portion_hint_too_long_rejected(self):
        """Test that portion_hint exceeding 255 chars is rejected."""
        with pytest.raises(ValidationError):
            ScheduleCreate(
                fish_id=uuid4(),
                time="12:00",
                interval_days=1,
                anchor_date=date.today(),
                food_type="flakes",
                portion_hint="A" * 256,
            )


class TestScheduleUpdate:
    """Tests for ScheduleUpdate schema."""

    def test_all_fields_optional(self):
        """Test that all fields are optional."""
        update = ScheduleUpdate()
        dump = update.model_dump(exclude_unset=True)
        assert dump == {}

    def test_partial_update_time(self):
        """Test partial update with time only."""
        update = ScheduleUpdate(time="14:00")
        assert update.time == "14:00"

    def test_partial_update_active(self):
        """Test partial update with active only."""
        update = ScheduleUpdate(active=False)
        assert update.active is False

    def test_partial_update_food_type_only(self):
        """Test partial update with food_type only."""
        update = ScheduleUpdate(food_type="frozen")
        assert update.food_type == "frozen"

    def test_invalid_time_format_rejected(self):
        """Test that invalid time format is rejected."""
        with pytest.raises(ValidationError):
            ScheduleUpdate(time="9am")

    def test_invalid_time_value_rejected(self):
        """Test that invalid time value is rejected."""
        with pytest.raises(ValidationError):
            ScheduleUpdate(time="25:00")


class TestScheduleResponse:
    """Tests for ScheduleResponse schema."""

    def test_valid_response(self):
        """Test valid ScheduleResponse creation."""
        now = datetime.now(UTC)
        response = ScheduleResponse(
            id=uuid4(),
            aquarium_id=uuid4(),
            fish_id=uuid4(),
            time="09:00",
            interval_days=1,
            anchor_date=date.today(),
            active=True,
            food_type="flakes",
            portion_hint="Small pinch",
            created_by_user_id=uuid4(),
            created_at=now,
            updated_at=now,
        )
        assert response.time == "09:00"
        assert response.active is True

    def test_from_orm_model(self):
        """Test ScheduleResponse creation from ORM model."""
        from datetime import time as dt_time

        class MockSchedule:
            def __init__(self):
                self.id = uuid4()
                self.aquarium_id = uuid4()
                self.fish_id = uuid4()
                self.time = dt_time(9, 0)
                self.interval_days = 1
                self.anchor_date = date.today()
                self.active = True
                self.food_type = "pellets"
                self.portion_hint = None
                self.created_by_user_id = None
                self.created_at = datetime.now(UTC)
                self.updated_at = datetime.now(UTC)

        mock = MockSchedule()
        response = ScheduleResponse.model_validate(mock)
        assert response.time == "09:00"
        assert response.fish_id == mock.fish_id


class TestFeedingLogCreate:
    """Tests for FeedingLogCreate schema."""

    def test_valid_fed_log(self):
        log = FeedingLogCreate(
            schedule_id=uuid4(),
            fish_id=uuid4(),
            scheduled_for=datetime.now(),
            action=FeedingAction.fed,
            device_id=uuid4(),
        )
        assert log.action == FeedingAction.fed
        assert log.notes is None

    def test_valid_skipped_log(self):
        log = FeedingLogCreate(
            schedule_id=uuid4(),
            fish_id=uuid4(),
            scheduled_for=datetime.now(),
            action=FeedingAction.skipped,
            device_id=uuid4(),
            notes="Fish looks unwell",
        )
        assert log.action == FeedingAction.skipped
        assert log.notes == "Fish looks unwell"

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            FeedingLogCreate(
                schedule_id=uuid4(),
                fish_id=uuid4(),
                scheduled_for=datetime.now(),
                action="invalid",
                device_id=uuid4(),
            )

    def test_notes_too_long_rejected(self):
        with pytest.raises(ValidationError):
            FeedingLogCreate(
                schedule_id=uuid4(),
                fish_id=uuid4(),
                scheduled_for=datetime.now(),
                action=FeedingAction.fed,
                device_id=uuid4(),
                notes="A" * 501,
            )


class TestFeedingLogResponse:
    """Tests for FeedingLogResponse schema."""

    def test_valid_response(self):
        now = datetime.now(UTC)
        response = FeedingLogResponse(
            id=uuid4(),
            schedule_id=uuid4(),
            fish_id=uuid4(),
            aquarium_id=uuid4(),
            scheduled_for=now,
            action="fed",
            acted_at=now,
            acted_by_user_id=uuid4(),
            device_id=uuid4(),
            notes=None,
            created_at=now,
        )
        assert response.action == "fed"
        assert response.acted_by_user_name is None

    def test_with_user_name(self):
        now = datetime.now(UTC)
        response = FeedingLogResponse(
            id=uuid4(),
            schedule_id=uuid4(),
            fish_id=uuid4(),
            aquarium_id=uuid4(),
            scheduled_for=now,
            action="skipped",
            acted_at=now,
            acted_by_user_id=uuid4(),
            device_id=uuid4(),
            notes="Forgot",
            created_at=now,
            acted_by_user_name="Alice",
        )
        assert response.acted_by_user_name == "Alice"
        assert response.notes == "Forgot"
