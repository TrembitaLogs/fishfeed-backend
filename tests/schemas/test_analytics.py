"""Tests for analytics Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.analytics import (
    DataExportResponse,
    EventBatchRequest,
    EventRequest,
    EventResponse,
    RateLimitInfo,
)


class TestEventRequest:
    """Tests for EventRequest schema."""

    def test_valid_event_minimal(self):
        """Test EventRequest with minimal required fields."""
        event = EventRequest(event_type="button_click")
        assert event.event_type == "button_click"
        assert event.properties == {}
        assert event.timestamp is None
        assert event.device_info is None

    def test_valid_event_full(self):
        """Test EventRequest with all fields."""
        now = datetime.now(UTC)
        event = EventRequest(
            event_type="page_view",
            properties={"page": "/home", "duration_ms": 1500},
            timestamp=now,
            device_info={"os": "iOS", "version": "17.0"},
        )
        assert event.event_type == "page_view"
        assert event.properties["page"] == "/home"
        assert event.timestamp == now
        assert event.device_info["os"] == "iOS"

    def test_event_type_snake_case_valid(self):
        """Test valid snake_case event types."""
        valid_types = [
            "click",
            "button_click",
            "user_signup_completed",
            "a1_test",
            "test123",
        ]
        for event_type in valid_types:
            event = EventRequest(event_type=event_type)
            assert event.event_type == event_type

    def test_event_type_invalid_pattern_rejected(self):
        """Test that invalid event_type patterns are rejected."""
        invalid_types = [
            "ButtonClick",  # PascalCase
            "button-click",  # kebab-case
            "123_event",  # starts with number
            "_underscore",  # starts with underscore
            "UPPERCASE",  # all uppercase
            "with space",  # has space
            "with.dot",  # has dot
        ]
        for event_type in invalid_types:
            with pytest.raises(ValidationError) as exc_info:
                EventRequest(event_type=event_type)
            errors = exc_info.value.errors()
            assert any(e["loc"] == ("event_type",) for e in errors)

    def test_event_type_empty_rejected(self):
        """Test that empty event_type is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            EventRequest(event_type="")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("event_type",) for e in errors)

    def test_event_type_too_long_rejected(self):
        """Test that event_type exceeding 100 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            EventRequest(event_type="a" * 101)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("event_type",) for e in errors)

    def test_event_type_max_length_accepted(self):
        """Test that event_type at 100 chars is accepted."""
        event = EventRequest(event_type="a" * 100)
        assert len(event.event_type) == 100

    def test_properties_flexible_types(self):
        """Test that properties accepts various value types."""
        event = EventRequest(
            event_type="test_event",
            properties={
                "string": "value",
                "number": 42,
                "float": 3.14,
                "boolean": True,
                "null": None,
                "array": [1, 2, 3],
                "nested": {"key": "value"},
            },
        )
        assert event.properties["string"] == "value"
        assert event.properties["number"] == 42
        assert event.properties["nested"]["key"] == "value"

    def test_json_serialization(self):
        """Test EventRequest can be serialized to JSON."""
        now = datetime.now(UTC)
        event = EventRequest(
            event_type="test_event",
            properties={"key": "value"},
            timestamp=now,
        )
        data = event.model_dump()
        assert data["event_type"] == "test_event"
        assert data["properties"]["key"] == "value"


class TestEventBatchRequest:
    """Tests for EventBatchRequest schema."""

    def test_valid_batch_single_event(self):
        """Test batch with single event."""
        batch = EventBatchRequest(
            events=[EventRequest(event_type="test_event")]
        )
        assert len(batch.events) == 1

    def test_valid_batch_multiple_events(self):
        """Test batch with multiple events."""
        events = [EventRequest(event_type=f"event_{i}") for i in range(10)]
        batch = EventBatchRequest(events=events)
        assert len(batch.events) == 10

    def test_batch_max_100_events_accepted(self):
        """Test batch with exactly 100 events is accepted."""
        events = [EventRequest(event_type=f"event_{i}") for i in range(100)]
        batch = EventBatchRequest(events=events)
        assert len(batch.events) == 100

    def test_batch_101_events_rejected(self):
        """Test that batch with 101 events is rejected."""
        events = [EventRequest(event_type=f"event_{i}") for i in range(101)]
        with pytest.raises(ValidationError) as exc_info:
            EventBatchRequest(events=events)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("events",) for e in errors)

    def test_batch_empty_rejected(self):
        """Test that empty batch is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            EventBatchRequest(events=[])
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("events",) for e in errors)

    def test_batch_with_invalid_event_rejected(self):
        """Test that batch containing invalid event is rejected."""
        with pytest.raises(ValidationError):
            EventBatchRequest(
                events=[
                    EventRequest(event_type="valid_event"),
                    {"event_type": "InvalidCase"},  # Invalid pattern
                ]
            )

    def test_json_serialization(self):
        """Test EventBatchRequest can be serialized to JSON."""
        batch = EventBatchRequest(
            events=[
                EventRequest(event_type="event_a", properties={"a": 1}),
                EventRequest(event_type="event_b", properties={"b": 2}),
            ]
        )
        data = batch.model_dump()
        assert len(data["events"]) == 2
        assert data["events"][0]["event_type"] == "event_a"


class TestEventResponse:
    """Tests for EventResponse schema."""

    def test_valid_accepted_status(self):
        """Test EventResponse with accepted status."""
        response = EventResponse(status="accepted", event_count=1)
        assert response.status == "accepted"
        assert response.event_count == 1

    def test_valid_queued_status(self):
        """Test EventResponse with queued status."""
        response = EventResponse(status="queued", event_count=50)
        assert response.status == "queued"
        assert response.event_count == 50

    def test_invalid_status_rejected(self):
        """Test that invalid status is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            EventResponse(status="invalid", event_count=1)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("status",) for e in errors)

    def test_event_count_zero_rejected(self):
        """Test that event_count of 0 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            EventResponse(status="accepted", event_count=0)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("event_count",) for e in errors)

    def test_event_count_negative_rejected(self):
        """Test that negative event_count is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            EventResponse(status="accepted", event_count=-1)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("event_count",) for e in errors)

    def test_json_serialization(self):
        """Test EventResponse can be serialized to JSON."""
        response = EventResponse(status="accepted", event_count=5)
        data = response.model_dump()
        assert data["status"] == "accepted"
        assert data["event_count"] == 5


class TestDataExportResponse:
    """Tests for DataExportResponse schema."""

    def test_valid_response(self):
        """Test valid DataExportResponse."""
        expires_at = datetime.now(UTC)
        response = DataExportResponse(
            download_url="https://s3.example.com/export/user123.json",
            expires_at=expires_at,
            file_size_bytes=1024,
            format="json",
        )
        assert str(response.download_url) == "https://s3.example.com/export/user123.json"
        assert response.expires_at == expires_at
        assert response.file_size_bytes == 1024
        assert response.format == "json"

    def test_default_format_json(self):
        """Test that format defaults to json."""
        response = DataExportResponse(
            download_url="https://example.com/file.json",
            expires_at=datetime.now(UTC),
            file_size_bytes=512,
        )
        assert response.format == "json"

    def test_invalid_url_rejected(self):
        """Test that invalid URL is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            DataExportResponse(
                download_url="not-a-url",
                expires_at=datetime.now(UTC),
                file_size_bytes=1024,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("download_url",) for e in errors)

    def test_negative_file_size_rejected(self):
        """Test that negative file_size_bytes is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            DataExportResponse(
                download_url="https://example.com/file.json",
                expires_at=datetime.now(UTC),
                file_size_bytes=-1,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("file_size_bytes",) for e in errors)

    def test_zero_file_size_accepted(self):
        """Test that file_size_bytes of 0 is accepted."""
        response = DataExportResponse(
            download_url="https://example.com/file.json",
            expires_at=datetime.now(UTC),
            file_size_bytes=0,
        )
        assert response.file_size_bytes == 0

    def test_json_serialization(self):
        """Test DataExportResponse can be serialized to JSON."""
        response = DataExportResponse(
            download_url="https://example.com/file.json",
            expires_at=datetime.now(UTC),
            file_size_bytes=2048,
        )
        data = response.model_dump()
        assert "download_url" in data
        assert data["file_size_bytes"] == 2048

    def test_json_schema_generation(self):
        """Test JSON Schema generation for OpenAPI."""
        schema = DataExportResponse.model_json_schema()
        assert "properties" in schema
        assert "download_url" in schema["properties"]
        assert "expires_at" in schema["properties"]
        assert "file_size_bytes" in schema["properties"]
        assert "format" in schema["properties"]


class TestRateLimitInfo:
    """Tests for RateLimitInfo schema."""

    def test_valid_rate_limit_info(self):
        """Test valid RateLimitInfo."""
        reset_at = datetime.now(UTC)
        info = RateLimitInfo(limit=100, remaining=75, reset_at=reset_at)
        assert info.limit == 100
        assert info.remaining == 75
        assert info.reset_at == reset_at

    def test_remaining_zero_accepted(self):
        """Test that remaining of 0 is accepted (limit reached)."""
        info = RateLimitInfo(
            limit=100,
            remaining=0,
            reset_at=datetime.now(UTC),
        )
        assert info.remaining == 0

    def test_limit_zero_rejected(self):
        """Test that limit of 0 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RateLimitInfo(limit=0, remaining=0, reset_at=datetime.now(UTC))
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("limit",) for e in errors)

    def test_negative_remaining_rejected(self):
        """Test that negative remaining is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RateLimitInfo(limit=100, remaining=-1, reset_at=datetime.now(UTC))
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("remaining",) for e in errors)

    def test_json_serialization(self):
        """Test RateLimitInfo can be serialized to JSON."""
        info = RateLimitInfo(
            limit=1000,
            remaining=999,
            reset_at=datetime.now(UTC),
        )
        data = info.model_dump()
        assert data["limit"] == 1000
        assert data["remaining"] == 999
        assert "reset_at" in data

    def test_json_schema_generation(self):
        """Test JSON Schema generation for OpenAPI."""
        schema = RateLimitInfo.model_json_schema()
        assert "properties" in schema
        assert "limit" in schema["properties"]
        assert "remaining" in schema["properties"]
        assert "reset_at" in schema["properties"]
