"""Tests for fish Pydantic schemas."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.fish import (
    FishCreate,
    FishResponse,
    FishUpdate,
)


class TestFishCreate:
    """Tests for FishCreate schema."""

    def test_valid_fish_create(self):
        """Test valid FishCreate with all fields."""
        fish = FishCreate(
            species_id="betta-splendens",
            quantity=5,
            custom_name="My Betta",
            added_via="manual",
        )
        assert fish.species_id == "betta-splendens"
        assert fish.quantity == 5
        assert fish.custom_name == "My Betta"
        assert fish.added_via == "manual"

    def test_minimal_fish_create(self):
        """Test FishCreate with minimal required fields."""
        fish = FishCreate(species_id="goldfish")
        assert fish.species_id == "goldfish"
        assert fish.quantity == 1
        assert fish.custom_name is None
        assert fish.added_via == "manual"

    def test_ai_scan_added_via(self):
        """Test FishCreate with ai_scan added_via."""
        fish = FishCreate(species_id="neon-tetra", added_via="ai_scan")
        assert fish.added_via == "ai_scan"

    def test_invalid_added_via_rejected(self):
        """Test that invalid added_via is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            FishCreate(species_id="test", added_via="unknown")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("added_via",)

    def test_quantity_zero_rejected(self):
        """Test that quantity of 0 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            FishCreate(species_id="test", quantity=0)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("quantity",)

    def test_quantity_negative_rejected(self):
        """Test that negative quantity is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            FishCreate(species_id="test", quantity=-1)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("quantity",)

    def test_quantity_one_accepted(self):
        """Test that quantity of 1 is accepted."""
        fish = FishCreate(species_id="test", quantity=1)
        assert fish.quantity == 1

    def test_empty_species_id_rejected(self):
        """Test that empty species_id is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            FishCreate(species_id="")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("species_id",)

    def test_species_id_too_long_rejected(self):
        """Test that species_id exceeding 50 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            FishCreate(species_id="a" * 51)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("species_id",)

    def test_custom_name_too_long_rejected(self):
        """Test that custom_name exceeding 100 chars is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            FishCreate(species_id="test", custom_name="A" * 101)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("custom_name",)


class TestFishUpdate:
    """Tests for FishUpdate schema."""

    def test_all_fields_optional(self):
        """Test that all fields are optional."""
        update = FishUpdate()
        assert update.quantity is None
        assert update.custom_name is None

    def test_partial_update_quantity(self):
        """Test partial update with quantity."""
        update = FishUpdate(quantity=10)
        assert update.quantity == 10
        assert update.custom_name is None

    def test_partial_update_custom_name(self):
        """Test partial update with custom_name."""
        update = FishUpdate(custom_name="New Name")
        assert update.custom_name == "New Name"
        assert update.quantity is None

    def test_quantity_zero_rejected(self):
        """Test that quantity of 0 is rejected in update."""
        with pytest.raises(ValidationError) as exc_info:
            FishUpdate(quantity=0)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("quantity",)

    def test_custom_name_too_long_rejected(self):
        """Test that custom_name exceeding 100 chars is rejected in update."""
        with pytest.raises(ValidationError) as exc_info:
            FishUpdate(custom_name="A" * 101)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("custom_name",)


class TestFishResponse:
    """Tests for FishResponse schema."""

    def test_valid_response(self):
        """Test valid FishResponse creation."""
        fish_id = uuid4()
        aquarium_id = uuid4()
        now = datetime.now(UTC)

        response = FishResponse(
            id=fish_id,
            aquarium_id=aquarium_id,
            species_id="betta-splendens",
            species=None,
            quantity=3,
            custom_name="Nemo",
            added_via="manual",
            created_at=now,
        )

        assert response.id == fish_id
        assert response.aquarium_id == aquarium_id
        assert response.species_id == "betta-splendens"
        assert response.species is None
        assert response.quantity == 3
        assert response.custom_name == "Nemo"
        assert response.added_via == "manual"
        assert response.created_at == now

    def test_from_orm_model(self):
        """Test FishResponse creation from ORM model."""

        class MockFish:
            def __init__(self):
                self.id = uuid4()
                self.aquarium_id = uuid4()
                self.species_id = "goldfish"
                self.species = None
                self.quantity = 2
                self.custom_name = None
                self.added_via = "ai_scan"
                self.created_at = datetime.now(UTC)

        mock = MockFish()
        response = FishResponse.model_validate(mock)

        assert response.id == mock.id
        assert response.aquarium_id == mock.aquarium_id
        assert response.species_id == mock.species_id
        assert response.quantity == mock.quantity
        assert response.added_via == mock.added_via

    def test_with_species_response(self):
        """Test FishResponse with nested SpeciesResponse."""
        from app.schemas.species import SpeciesResponse

        fish_id = uuid4()
        aquarium_id = uuid4()
        now = datetime.now(UTC)

        species = SpeciesResponse(
            id="betta",
            common_name="Betta",
            scientific_name="Betta splendens",
            image_url=None,
            food_types=["pellets"],
            feeding_frequency=2,
            portion_hint=None,
            care_level="beginner",
            water_type="freshwater",
            metadata=None,
            created_at=now,
            updated_at=now,
        )

        response = FishResponse(
            id=fish_id,
            aquarium_id=aquarium_id,
            species_id="betta",
            species=species,
            quantity=1,
            custom_name=None,
            added_via="manual",
            created_at=now,
        )

        assert response.species is not None
        assert response.species.common_name == "Betta"

    def test_json_serialization(self):
        """Test FishResponse can be serialized to JSON."""
        now = datetime.now(UTC)

        response = FishResponse(
            id=uuid4(),
            aquarium_id=uuid4(),
            species_id="test",
            species=None,
            quantity=1,
            custom_name="Test Fish",
            added_via="manual",
            created_at=now,
        )

        data = response.model_dump()
        assert data["species_id"] == "test"
        assert data["custom_name"] == "Test Fish"
        assert data["quantity"] == 1
