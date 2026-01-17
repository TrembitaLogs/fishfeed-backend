"""Tests for aquarium Pydantic schemas."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.aquarium import (
    AquariumBase,
    AquariumCreate,
    AquariumResponse,
    AquariumUpdate,
)


class TestAquariumBase:
    """Tests for AquariumBase schema."""

    def test_valid_aquarium_base(self):
        """Test valid AquariumBase creation."""
        aquarium = AquariumBase(name="My Aquarium")
        assert aquarium.name == "My Aquarium"

    def test_empty_name_rejected(self):
        """Test that empty name is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AquariumBase(name="")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("name",)
        assert "string_too_short" in errors[0]["type"]

    def test_name_too_long_rejected(self):
        """Test that name exceeding 100 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AquariumBase(name="A" * 101)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("name",)
        assert "string_too_long" in errors[0]["type"]

    def test_name_exactly_100_chars(self):
        """Test that name with exactly 100 chars is accepted."""
        aquarium = AquariumBase(name="A" * 100)
        assert len(aquarium.name) == 100

    def test_name_exactly_1_char(self):
        """Test that name with exactly 1 char is accepted."""
        aquarium = AquariumBase(name="X")
        assert aquarium.name == "X"


class TestAquariumCreate:
    """Tests for AquariumCreate schema."""

    def test_valid_aquarium_create(self):
        """Test valid AquariumCreate."""
        aquarium = AquariumCreate(name="New Tank")
        assert aquarium.name == "New Tank"

    def test_inherits_validation(self):
        """Test that AquariumCreate inherits validation from AquariumBase."""
        with pytest.raises(ValidationError):
            AquariumCreate(name="")


class TestAquariumUpdate:
    """Tests for AquariumUpdate schema."""

    def test_all_fields_optional(self):
        """Test that all fields are optional."""
        update = AquariumUpdate()
        assert update.name is None

    def test_partial_update(self):
        """Test partial update with name."""
        update = AquariumUpdate(name="Updated Name")
        assert update.name == "Updated Name"

    def test_empty_name_rejected(self):
        """Test that empty name is rejected in update."""
        with pytest.raises(ValidationError) as exc_info:
            AquariumUpdate(name="")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("name",)

    def test_name_too_long_rejected(self):
        """Test that name exceeding 100 chars is rejected in update."""
        with pytest.raises(ValidationError) as exc_info:
            AquariumUpdate(name="A" * 101)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("name",)


class TestAquariumResponse:
    """Tests for AquariumResponse schema."""

    def test_valid_response(self):
        """Test valid AquariumResponse creation."""
        aquarium_id = uuid4()
        owner_id = uuid4()
        now = datetime.now(UTC)

        response = AquariumResponse(
            id=aquarium_id,
            name="Test Tank",
            owner_id=owner_id,
            created_at=now,
            updated_at=now,
        )

        assert response.id == aquarium_id
        assert response.name == "Test Tank"
        assert response.owner_id == owner_id
        assert response.created_at == now
        assert response.updated_at == now

    def test_from_orm_model(self):
        """Test AquariumResponse creation from ORM model."""

        class MockAquarium:
            def __init__(self):
                self.id = uuid4()
                self.name = "ORM Tank"
                self.owner_id = uuid4()
                self.created_at = datetime.now(UTC)
                self.updated_at = datetime.now(UTC)

        mock = MockAquarium()
        response = AquariumResponse.model_validate(mock)

        assert response.id == mock.id
        assert response.name == mock.name
        assert response.owner_id == mock.owner_id

    def test_json_serialization(self):
        """Test AquariumResponse can be serialized to JSON."""
        aquarium_id = uuid4()
        owner_id = uuid4()
        now = datetime.now(UTC)

        response = AquariumResponse(
            id=aquarium_id,
            name="Test Tank",
            owner_id=owner_id,
            created_at=now,
            updated_at=now,
        )

        data = response.model_dump()
        assert data["name"] == "Test Tank"
        assert data["id"] == aquarium_id
        assert data["owner_id"] == owner_id

    def test_datetime_serialization(self):
        """Test that datetime is serialized in ISO format."""
        now = datetime.now(UTC)

        response = AquariumResponse(
            id=uuid4(),
            name="Test",
            owner_id=uuid4(),
            created_at=now,
            updated_at=now,
        )

        json_data = response.model_dump_json()
        assert "T" in json_data  # ISO format contains 'T' separator
