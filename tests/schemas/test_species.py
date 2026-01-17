"""Tests for species Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.species import (
    SpeciesBase,
    SpeciesCreate,
    SpeciesListResponse,
    SpeciesResponse,
    SpeciesSearchQuery,
    SpeciesUpdate,
)


class TestSpeciesBase:
    """Tests for SpeciesBase schema."""

    def test_valid_species_base(self):
        """Test valid SpeciesBase creation."""
        species = SpeciesBase(
            common_name="Betta",
            scientific_name="Betta splendens",
            food_types=["flakes", "pellets"],
            feeding_frequency=2,
        )
        assert species.common_name == "Betta"
        assert species.scientific_name == "Betta splendens"
        assert species.food_types == ["flakes", "pellets"]
        assert species.feeding_frequency == 2

    def test_minimal_species_base(self):
        """Test SpeciesBase with minimal required fields."""
        species = SpeciesBase(common_name="Goldfish")
        assert species.common_name == "Goldfish"
        assert species.scientific_name is None
        assert species.food_types == []
        assert species.feeding_frequency == 2

    def test_empty_common_name_rejected(self):
        """Test that empty common_name is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesBase(common_name="")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("common_name",)
        assert "string_too_short" in errors[0]["type"]

    def test_common_name_too_long(self):
        """Test that common_name exceeding 100 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesBase(common_name="A" * 101)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("common_name",)
        assert "string_too_long" in errors[0]["type"]

    def test_invalid_food_type_rejected(self):
        """Test that invalid food type is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesBase(common_name="Betta", food_types=["invalid_food"])

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("food_types", 0)

    def test_feeding_frequency_too_low(self):
        """Test that feeding_frequency below 1 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesBase(common_name="Betta", feeding_frequency=0)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("feeding_frequency",)

    def test_feeding_frequency_too_high(self):
        """Test that feeding_frequency above 10 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesBase(common_name="Betta", feeding_frequency=11)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("feeding_frequency",)


class TestSpeciesCreate:
    """Tests for SpeciesCreate schema."""

    def test_valid_species_create(self):
        """Test valid SpeciesCreate with all fields."""
        species = SpeciesCreate(
            id="betta-splendens",
            common_name="Betta",
            scientific_name="Betta splendens",
            food_types=["pellets", "frozen"],
            feeding_frequency=2,
            image_url="https://example.com/betta.jpg",
            portion_hint="2-3 pellets per fish",
            care_level="beginner",
            water_type="freshwater",
            metadata={"min_temp": 24, "max_temp": 28},
        )
        assert species.id == "betta-splendens"
        assert species.care_level == "beginner"
        assert species.water_type == "freshwater"
        assert species.metadata == {"min_temp": 24, "max_temp": 28}

    def test_minimal_species_create(self):
        """Test SpeciesCreate with minimal required fields."""
        species = SpeciesCreate(id="goldfish", common_name="Goldfish")
        assert species.id == "goldfish"
        assert species.common_name == "Goldfish"
        assert species.care_level == "beginner"
        assert species.water_type == "freshwater"

    def test_invalid_care_level_rejected(self):
        """Test that invalid care_level is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesCreate(
                id="test",
                common_name="Test Fish",
                care_level="expert",
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("care_level",)

    def test_invalid_water_type_rejected(self):
        """Test that invalid water_type is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesCreate(
                id="test",
                common_name="Test Fish",
                water_type="ocean",
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("water_type",)

    def test_empty_id_rejected(self):
        """Test that empty id is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesCreate(id="", common_name="Test Fish")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("id",)

    def test_id_too_long_rejected(self):
        """Test that id exceeding 50 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesCreate(id="a" * 51, common_name="Test Fish")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("id",)


class TestSpeciesUpdate:
    """Tests for SpeciesUpdate schema."""

    def test_all_fields_optional(self):
        """Test that all fields are optional."""
        update = SpeciesUpdate()
        assert update.common_name is None
        assert update.scientific_name is None
        assert update.food_types is None
        assert update.feeding_frequency is None
        assert update.care_level is None

    def test_partial_update(self):
        """Test partial update with some fields."""
        update = SpeciesUpdate(
            common_name="Updated Betta",
            care_level="intermediate",
        )
        assert update.common_name == "Updated Betta"
        assert update.care_level == "intermediate"
        assert update.water_type is None

    def test_invalid_care_level_in_update(self):
        """Test that invalid care_level in update is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesUpdate(care_level="expert")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("care_level",)

    def test_empty_food_types_rejected(self):
        """Test that empty food_types list is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesUpdate(food_types=[])

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("food_types",)
        assert "empty" in str(errors[0]["msg"]).lower()


class TestSpeciesResponse:
    """Tests for SpeciesResponse schema."""

    def test_valid_response(self):
        """Test valid SpeciesResponse creation."""
        now = datetime.now(UTC)
        response = SpeciesResponse(
            id="betta-splendens",
            common_name="Betta",
            scientific_name="Betta splendens",
            image_url="https://example.com/betta.jpg",
            food_types=["pellets", "frozen"],
            feeding_frequency=2,
            portion_hint="2-3 pellets",
            care_level="beginner",
            water_type="freshwater",
            metadata={"min_temp": 24},
            created_at=now,
            updated_at=now,
        )
        assert response.id == "betta-splendens"
        assert response.common_name == "Betta"
        assert response.metadata == {"min_temp": 24}

    def test_from_orm_model(self):
        """Test SpeciesResponse creation from ORM model."""

        class MockSpecies:
            def __init__(self):
                self.id = "goldfish"
                self.common_name = "Goldfish"
                self.scientific_name = "Carassius auratus"
                self.image_url = None
                self.food_types = ["flakes"]
                self.feeding_frequency = 2
                self.portion_hint = None
                self.care_level = "beginner"
                self.water_type = "freshwater"
                self.metadata_ = {"ph": "7.0-7.5"}
                self.created_at = datetime.now(UTC)
                self.updated_at = datetime.now(UTC)

        mock = MockSpecies()
        response = SpeciesResponse.model_validate(mock)

        assert response.id == mock.id
        assert response.common_name == mock.common_name
        assert response.metadata == {"ph": "7.0-7.5"}

    def test_json_serialization(self):
        """Test SpeciesResponse can be serialized to JSON."""
        now = datetime.now(UTC)
        response = SpeciesResponse(
            id="test",
            common_name="Test Fish",
            scientific_name=None,
            image_url=None,
            food_types=["flakes"],
            feeding_frequency=1,
            portion_hint=None,
            care_level="beginner",
            water_type="freshwater",
            metadata=None,
            created_at=now,
            updated_at=now,
        )

        data = response.model_dump()
        assert data["id"] == "test"
        assert data["common_name"] == "Test Fish"
        assert data["food_types"] == ["flakes"]


class TestSpeciesListResponse:
    """Tests for SpeciesListResponse schema."""

    def test_pages_calculation(self):
        """Test that pages is correctly calculated."""
        response = SpeciesListResponse(
            items=[],
            total=100,
            page=1,
            per_page=10,
        )
        assert response.pages == 10

    def test_pages_calculation_with_remainder(self):
        """Test pages calculation with remainder."""
        response = SpeciesListResponse(
            items=[],
            total=95,
            page=1,
            per_page=10,
        )
        assert response.pages == 10

    def test_pages_zero_total(self):
        """Test pages calculation with zero total."""
        response = SpeciesListResponse(
            items=[],
            total=0,
            page=1,
            per_page=10,
        )
        assert response.pages == 0

    def test_pages_single_page(self):
        """Test pages calculation for single page."""
        response = SpeciesListResponse(
            items=[],
            total=5,
            page=1,
            per_page=10,
        )
        assert response.pages == 1

    def test_invalid_page_number(self):
        """Test that page number below 1 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesListResponse(
                items=[],
                total=10,
                page=0,
                per_page=10,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("page",) for e in errors)

    def test_invalid_per_page(self):
        """Test that per_page above 100 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesListResponse(
                items=[],
                total=10,
                page=1,
                per_page=101,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("per_page",) for e in errors)


class TestSpeciesSearchQuery:
    """Tests for SpeciesSearchQuery schema."""

    def test_empty_query(self):
        """Test empty search query."""
        query = SpeciesSearchQuery()
        assert query.q is None
        assert query.care_level is None
        assert query.water_type is None

    def test_full_query(self):
        """Test search query with all fields."""
        query = SpeciesSearchQuery(
            q="betta",
            care_level="beginner",
            water_type="freshwater",
        )
        assert query.q == "betta"
        assert query.care_level == "beginner"
        assert query.water_type == "freshwater"

    def test_query_too_long(self):
        """Test that query exceeding 100 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesSearchQuery(q="a" * 101)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("q",)

    def test_invalid_care_level_in_query(self):
        """Test that invalid care_level in query is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesSearchQuery(care_level="expert")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("care_level",)

    def test_invalid_water_type_in_query(self):
        """Test that invalid water_type in query is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SpeciesSearchQuery(water_type="ocean")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("water_type",)
