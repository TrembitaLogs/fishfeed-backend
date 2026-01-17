"""Tests for feeding Pydantic schemas."""

from datetime import UTC, datetime, time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.feeding import (
    EventResponse,
    ScheduleCreate,
    ScheduleResponse,
    ScheduleUpdate,
    TodayEventsResponse,
)


class TestScheduleCreate:
    """Tests for ScheduleCreate schema."""

    def test_valid_schedule_create(self):
        """Test valid ScheduleCreate with all fields."""
        schedule = ScheduleCreate(
            times_per_day=3,
            scheduled_times=[time(8, 0), time(13, 0), time(19, 0)],
            food_type="pellets",
            portion_hint="2-3 pellets per fish",
        )
        assert schedule.times_per_day == 3
        assert len(schedule.scheduled_times) == 3
        assert schedule.food_type == "pellets"
        assert schedule.portion_hint == "2-3 pellets per fish"

    def test_minimal_schedule_create(self):
        """Test ScheduleCreate with minimal required fields."""
        schedule = ScheduleCreate(
            times_per_day=1,
            scheduled_times=[time(12, 0)],
        )
        assert schedule.times_per_day == 1
        assert schedule.food_type is None
        assert schedule.portion_hint is None

    def test_times_per_day_mismatch_rejected(self):
        """Test that times_per_day != len(scheduled_times) is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                times_per_day=3,
                scheduled_times=[time(8, 0), time(12, 0)],  # Only 2 times
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert "times_per_day" in str(errors[0]["msg"])

    def test_times_auto_sorted(self):
        """Test that scheduled_times are automatically sorted."""
        schedule = ScheduleCreate(
            times_per_day=3,
            scheduled_times=[time(19, 0), time(8, 0), time(13, 0)],
        )
        assert schedule.scheduled_times == [time(8, 0), time(13, 0), time(19, 0)]

    def test_duplicate_times_removed(self):
        """Test that duplicate times are removed."""
        schedule = ScheduleCreate(
            times_per_day=2,
            scheduled_times=[time(8, 0), time(8, 0), time(12, 0)],
        )
        assert len(schedule.scheduled_times) == 2
        assert schedule.scheduled_times == [time(8, 0), time(12, 0)]

    def test_times_per_day_zero_rejected(self):
        """Test that times_per_day of 0 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                times_per_day=0,
                scheduled_times=[],
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("times_per_day",) for e in errors)

    def test_times_per_day_above_10_rejected(self):
        """Test that times_per_day above 10 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                times_per_day=11,
                scheduled_times=[time(i, 0) for i in range(11)],
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("times_per_day",) for e in errors)

    def test_empty_scheduled_times_rejected(self):
        """Test that empty scheduled_times is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                times_per_day=1,
                scheduled_times=[],
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("scheduled_times",) for e in errors)

    def test_food_type_too_long_rejected(self):
        """Test that food_type exceeding 50 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                times_per_day=1,
                scheduled_times=[time(12, 0)],
                food_type="A" * 51,
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("food_type",)

    def test_portion_hint_too_long_rejected(self):
        """Test that portion_hint exceeding 255 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                times_per_day=1,
                scheduled_times=[time(12, 0)],
                portion_hint="A" * 256,
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("portion_hint",)


class TestScheduleUpdate:
    """Tests for ScheduleUpdate schema."""

    def test_all_fields_optional(self):
        """Test that all fields are optional."""
        update = ScheduleUpdate()
        assert update.times_per_day is None
        assert update.scheduled_times is None
        assert update.food_type is None
        assert update.portion_hint is None

    def test_partial_update_times(self):
        """Test partial update with times."""
        update = ScheduleUpdate(
            times_per_day=2,
            scheduled_times=[time(9, 0), time(18, 0)],
        )
        assert update.times_per_day == 2
        assert len(update.scheduled_times) == 2

    def test_partial_update_food_type_only(self):
        """Test partial update with food_type only."""
        update = ScheduleUpdate(food_type="frozen")
        assert update.food_type == "frozen"
        assert update.times_per_day is None

    def test_times_auto_sorted_in_update(self):
        """Test that scheduled_times are automatically sorted in update."""
        update = ScheduleUpdate(
            scheduled_times=[time(18, 0), time(9, 0)],
        )
        assert update.scheduled_times == [time(9, 0), time(18, 0)]

    def test_mismatch_times_rejected_when_both_provided(self):
        """Test that mismatched times_per_day is rejected when both provided."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleUpdate(
                times_per_day=3,
                scheduled_times=[time(9, 0), time(18, 0)],  # Only 2 times
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert "times_per_day" in str(errors[0]["msg"])

    def test_times_per_day_alone_accepted(self):
        """Test that times_per_day alone is accepted."""
        update = ScheduleUpdate(times_per_day=3)
        assert update.times_per_day == 3

    def test_scheduled_times_alone_accepted(self):
        """Test that scheduled_times alone is accepted."""
        update = ScheduleUpdate(scheduled_times=[time(10, 0)])
        assert len(update.scheduled_times) == 1


class TestScheduleResponse:
    """Tests for ScheduleResponse schema."""

    def test_valid_response(self):
        """Test valid ScheduleResponse creation."""
        schedule_id = uuid4()
        aquarium_id = uuid4()
        now = datetime.now(UTC)

        response = ScheduleResponse(
            id=schedule_id,
            aquarium_id=aquarium_id,
            times_per_day=2,
            scheduled_times=["08:00", "18:00"],
            food_type="flakes",
            portion_hint="Small pinch",
            created_at=now,
            updated_at=now,
        )

        assert response.id == schedule_id
        assert response.aquarium_id == aquarium_id
        assert response.times_per_day == 2
        assert response.scheduled_times == ["08:00", "18:00"]
        assert response.food_type == "flakes"

    def test_from_orm_model(self):
        """Test ScheduleResponse creation from ORM model."""

        class MockSchedule:
            def __init__(self):
                self.id = uuid4()
                self.aquarium_id = uuid4()
                self.times_per_day = 3
                self.scheduled_times = ["09:00", "14:00", "19:00"]
                self.food_type = "pellets"
                self.portion_hint = None
                self.created_at = datetime.now(UTC)
                self.updated_at = datetime.now(UTC)

        mock = MockSchedule()
        response = ScheduleResponse.model_validate(mock)

        assert response.id == mock.id
        assert response.times_per_day == mock.times_per_day
        assert response.scheduled_times == mock.scheduled_times

    def test_json_serialization(self):
        """Test ScheduleResponse can be serialized to JSON."""
        now = datetime.now(UTC)

        response = ScheduleResponse(
            id=uuid4(),
            aquarium_id=uuid4(),
            times_per_day=1,
            scheduled_times=["12:00"],
            food_type="flakes",
            portion_hint=None,
            created_at=now,
            updated_at=now,
        )

        data = response.model_dump()
        assert data["times_per_day"] == 1
        assert data["scheduled_times"] == ["12:00"]


class TestEventResponse:
    """Tests for EventResponse schema."""

    def test_valid_pending_event(self):
        """Test valid EventResponse with pending status."""
        event_id = uuid4()
        aquarium_id = uuid4()
        schedule_id = uuid4()
        scheduled_at = datetime.now(UTC)

        response = EventResponse(
            id=event_id,
            aquarium_id=aquarium_id,
            schedule_id=schedule_id,
            scheduled_at=scheduled_at,
            status="pending",
            completed_at=None,
            completed_by=None,
        )

        assert response.id == event_id
        assert response.status == "pending"
        assert response.completed_at is None
        assert response.completed_by is None

    def test_valid_completed_event(self):
        """Test valid EventResponse with completed status."""
        event_id = uuid4()
        aquarium_id = uuid4()
        schedule_id = uuid4()
        user_id = uuid4()
        now = datetime.now(UTC)

        response = EventResponse(
            id=event_id,
            aquarium_id=aquarium_id,
            schedule_id=schedule_id,
            scheduled_at=now,
            status="completed",
            completed_at=now,
            completed_by=user_id,
        )

        assert response.status == "completed"
        assert response.completed_at == now
        assert response.completed_by == user_id

    def test_valid_missed_event(self):
        """Test valid EventResponse with missed status."""
        response = EventResponse(
            id=uuid4(),
            aquarium_id=uuid4(),
            schedule_id=uuid4(),
            scheduled_at=datetime.now(UTC),
            status="missed",
            completed_at=None,
            completed_by=None,
        )

        assert response.status == "missed"

    def test_invalid_status_rejected(self):
        """Test that invalid status is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            EventResponse(
                id=uuid4(),
                aquarium_id=uuid4(),
                schedule_id=uuid4(),
                scheduled_at=datetime.now(UTC),
                status="unknown",
                completed_at=None,
                completed_by=None,
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("status",)

    def test_nullable_schedule_id(self):
        """Test that schedule_id can be None."""
        response = EventResponse(
            id=uuid4(),
            aquarium_id=uuid4(),
            schedule_id=None,
            scheduled_at=datetime.now(UTC),
            status="pending",
            completed_at=None,
            completed_by=None,
        )

        assert response.schedule_id is None

    def test_from_orm_model(self):
        """Test EventResponse creation from ORM model."""

        class MockEvent:
            def __init__(self):
                self.id = uuid4()
                self.aquarium_id = uuid4()
                self.schedule_id = uuid4()
                self.scheduled_at = datetime.now(UTC)
                self.status = "pending"
                self.completed_at = None
                self.completed_by = None

        mock = MockEvent()
        response = EventResponse.model_validate(mock)

        assert response.id == mock.id
        assert response.status == mock.status

    def test_datetime_serialization(self):
        """Test that datetime is serialized in ISO format."""
        now = datetime.now(UTC)

        response = EventResponse(
            id=uuid4(),
            aquarium_id=uuid4(),
            schedule_id=uuid4(),
            scheduled_at=now,
            status="pending",
            completed_at=None,
            completed_by=None,
        )

        json_data = response.model_dump_json()
        assert "T" in json_data  # ISO format contains 'T' separator


class TestTodayEventsResponse:
    """Tests for TodayEventsResponse schema."""

    def test_empty_events(self):
        """Test TodayEventsResponse with no events."""
        response = TodayEventsResponse(events=[], next_feeding=None)
        assert response.events == []
        assert response.next_feeding is None

    def test_with_events(self):
        """Test TodayEventsResponse with events."""
        now = datetime.now(UTC)
        event = EventResponse(
            id=uuid4(),
            aquarium_id=uuid4(),
            schedule_id=uuid4(),
            scheduled_at=now,
            status="pending",
            completed_at=None,
            completed_by=None,
        )

        response = TodayEventsResponse(
            events=[event],
            next_feeding=now,
        )

        assert len(response.events) == 1
        assert response.next_feeding == now

    def test_default_values(self):
        """Test TodayEventsResponse default values."""
        response = TodayEventsResponse()
        assert response.events == []
        assert response.next_feeding is None

    def test_json_serialization(self):
        """Test TodayEventsResponse can be serialized to JSON."""
        now = datetime.now(UTC)
        event = EventResponse(
            id=uuid4(),
            aquarium_id=uuid4(),
            schedule_id=uuid4(),
            scheduled_at=now,
            status="completed",
            completed_at=now,
            completed_by=uuid4(),
        )

        response = TodayEventsResponse(
            events=[event],
            next_feeding=now,
        )

        data = response.model_dump()
        assert len(data["events"]) == 1
        assert data["events"][0]["status"] == "completed"
