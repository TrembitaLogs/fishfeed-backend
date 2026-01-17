"""Unit tests for notification schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.notification import (
    NotificationPreferencesResponse,
    NotificationPreferencesUpdate,
    PushTokenRequest,
    PushTokenResponse,
)


class TestPushTokenRequest:
    """Tests for PushTokenRequest schema."""

    def test_valid_ios_token(self):
        """Test valid iOS push token."""
        data = PushTokenRequest(
            token="abc123devicetoken",
            platform="ios",
        )
        assert data.token == "abc123devicetoken"
        assert data.platform == "ios"

    def test_valid_android_token(self):
        """Test valid Android push token."""
        data = PushTokenRequest(
            token="fcm:abc123token",
            platform="android",
        )
        assert data.token == "fcm:abc123token"
        assert data.platform == "android"

    def test_empty_token_fails(self):
        """Test that empty token fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            PushTokenRequest(token="", platform="ios")
        assert "String should have at least 1 character" in str(exc_info.value)

    def test_invalid_platform_fails(self):
        """Test that invalid platform fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            PushTokenRequest(token="token123", platform="windows")
        assert "Input should be 'ios' or 'android'" in str(exc_info.value)

    def test_token_max_length(self):
        """Test token max length validation."""
        with pytest.raises(ValidationError) as exc_info:
            PushTokenRequest(token="x" * 513, platform="ios")
        assert "String should have at most 512 characters" in str(exc_info.value)


class TestPushTokenResponse:
    """Tests for PushTokenResponse schema."""

    def test_from_dict(self):
        """Test creating response from dict."""
        from uuid import uuid4

        token_id = uuid4()
        now = datetime.now(UTC)

        data = PushTokenResponse(
            id=token_id,
            token="test_token",
            platform="ios",
            created_at=now,
        )

        assert data.id == token_id
        assert data.token == "test_token"
        assert data.platform == "ios"
        assert data.created_at == now


class TestNotificationPreferencesUpdate:
    """Tests for NotificationPreferencesUpdate schema."""

    def test_all_fields_optional(self):
        """Test that all fields are optional."""
        data = NotificationPreferencesUpdate()
        assert data.feeding_reminders is None
        assert data.overdue_alerts is None
        assert data.streak_protection is None
        assert data.weekly_summary is None
        assert data.family_updates is None
        assert data.marketing is None

    def test_partial_update(self):
        """Test partial update with some fields."""
        data = NotificationPreferencesUpdate(
            feeding_reminders=False,
            marketing=True,
        )
        assert data.feeding_reminders is False
        assert data.marketing is True
        assert data.overdue_alerts is None
        assert data.weekly_summary is None

    def test_all_fields_set(self):
        """Test setting all fields."""
        data = NotificationPreferencesUpdate(
            feeding_reminders=True,
            overdue_alerts=False,
            streak_protection=True,
            weekly_summary=False,
            family_updates=True,
            marketing=False,
        )
        assert data.feeding_reminders is True
        assert data.overdue_alerts is False
        assert data.streak_protection is True
        assert data.weekly_summary is False
        assert data.family_updates is True
        assert data.marketing is False


class TestNotificationPreferencesResponse:
    """Tests for NotificationPreferencesResponse schema."""

    def test_all_fields_required_except_updated_at(self):
        """Test that all fields except updated_at and timezone are required."""
        data = NotificationPreferencesResponse(
            global_opt_out=False,
            feeding_reminders=True,
            overdue_alerts=True,
            streak_protection=True,
            weekly_summary=True,
            family_updates=True,
            marketing=False,
        )
        assert data.updated_at is None
        assert data.timezone is None

    def test_with_updated_at(self):
        """Test response with updated_at."""
        now = datetime.now(UTC)
        data = NotificationPreferencesResponse(
            global_opt_out=False,
            timezone="+02:00",
            feeding_reminders=True,
            overdue_alerts=True,
            streak_protection=True,
            weekly_summary=True,
            family_updates=True,
            marketing=False,
            updated_at=now,
        )
        assert data.updated_at == now
        assert data.timezone == "+02:00"
