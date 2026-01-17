"""Tests for AI Pydantic schemas."""

import base64
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.ai import (
    AlternativeSpecies,
    ScanConfirmRequest,
    ScanRequest,
    ScanResponse,
    ScansRemainingResponse,
)


# Helper for generating test species IDs
def make_species_id() -> str:
    """Generate a test species ID string."""
    return f"test-species-{uuid4().hex[:8]}"


class TestScanRequest:
    """Tests for ScanRequest schema."""

    def test_valid_scan_request_with_base64(self):
        """Test valid ScanRequest with base64 image."""
        image_data = base64.b64encode(b"fake image data").decode()
        request = ScanRequest(image_base64=image_data)
        assert request.image_base64 == image_data

    def test_scan_request_with_none_image(self):
        """Test ScanRequest with None image (for UploadFile usage)."""
        request = ScanRequest(image_base64=None)
        assert request.image_base64 is None

    def test_scan_request_empty_init(self):
        """Test ScanRequest with no arguments."""
        request = ScanRequest()
        assert request.image_base64 is None

    def test_scan_request_with_empty_string(self):
        """Test ScanRequest accepts empty string (validation at service level)."""
        request = ScanRequest(image_base64="")
        assert request.image_base64 == ""


class TestAlternativeSpecies:
    """Tests for AlternativeSpecies schema."""

    def test_valid_alternative_species(self):
        """Test valid AlternativeSpecies creation."""
        species_id = "betta"
        alt = AlternativeSpecies(
            species_id=species_id,
            species_name="Betta splendens",
            confidence=0.85,
        )
        assert alt.species_id == species_id
        assert alt.species_name == "Betta splendens"
        assert alt.confidence == 0.85

    def test_confidence_at_zero(self):
        """Test confidence at minimum value."""
        alt = AlternativeSpecies(
            species_id="test-fish",
            species_name="Test Fish",
            confidence=0.0,
        )
        assert alt.confidence == 0.0

    def test_confidence_at_one(self):
        """Test confidence at maximum value."""
        alt = AlternativeSpecies(
            species_id="test-fish",
            species_name="Test Fish",
            confidence=1.0,
        )
        assert alt.confidence == 1.0

    def test_confidence_below_zero_rejected(self):
        """Test that confidence below 0 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AlternativeSpecies(
                species_id="test-fish",
                species_name="Test Fish",
                confidence=-0.1,
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("confidence",)

    def test_confidence_above_one_rejected(self):
        """Test that confidence above 1 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AlternativeSpecies(
                species_id="test-fish",
                species_name="Test Fish",
                confidence=1.1,
            )

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("confidence",)

    def test_missing_required_fields_rejected(self):
        """Test that missing required fields are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AlternativeSpecies(species_id="test-fish")

        errors = exc_info.value.errors()
        field_names = {e["loc"][0] for e in errors}
        assert "species_name" in field_names
        assert "confidence" in field_names

    def test_json_serialization(self):
        """Test AlternativeSpecies serialization to dict."""
        species_id = "goldfish"
        alt = AlternativeSpecies(
            species_id=species_id,
            species_name="Goldfish",
            confidence=0.75,
        )
        data = alt.model_dump()
        assert data["species_id"] == species_id
        assert data["species_name"] == "Goldfish"
        assert data["confidence"] == 0.75


class TestScanResponse:
    """Tests for ScanResponse schema."""

    def test_valid_scan_response_full(self):
        """Test valid ScanResponse with all fields."""
        scan_id = uuid4()
        species_id = "betta"
        alt_species_id = "guppy"

        response = ScanResponse(
            scan_id=scan_id,
            species_id=species_id,
            species_name="Betta",
            confidence=0.92,
            alternatives=[
                AlternativeSpecies(
                    species_id=alt_species_id,
                    species_name="Guppy",
                    confidence=0.65,
                )
            ],
            scans_remaining=4,
            image_url="https://storage.example.com/scans/123.jpg",
        )

        assert response.scan_id == scan_id
        assert response.species_id == species_id
        assert response.species_name == "Betta"
        assert response.confidence == 0.92
        assert len(response.alternatives) == 1
        assert response.alternatives[0].species_name == "Guppy"
        assert response.scans_remaining == 4
        assert response.image_url == "https://storage.example.com/scans/123.jpg"

    def test_scan_response_minimal(self):
        """Test ScanResponse with minimal required fields."""
        scan_id = uuid4()
        response = ScanResponse(
            scan_id=scan_id,
            confidence=0.0,
            scans_remaining=5,
        )

        assert response.scan_id == scan_id
        assert response.species_id is None
        assert response.species_name is None
        assert response.confidence == 0.0
        assert response.alternatives == []
        assert response.scans_remaining == 5
        assert response.image_url is None

    def test_scan_response_confidence_validation(self):
        """Test that confidence must be in valid range."""
        with pytest.raises(ValidationError) as exc_info:
            ScanResponse(
                scan_id=uuid4(),
                confidence=1.5,
                scans_remaining=5,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("confidence",) for e in errors)

    def test_scan_response_negative_scans_rejected(self):
        """Test that negative scans_remaining is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScanResponse(
                scan_id=uuid4(),
                confidence=0.5,
                scans_remaining=-1,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("scans_remaining",) for e in errors)

    def test_scan_response_with_multiple_alternatives(self):
        """Test ScanResponse with top-3 alternatives."""
        alternatives = [
            AlternativeSpecies(
                species_id=f"fish-{i}",
                species_name=f"Fish {i}",
                confidence=0.9 - i * 0.1,
            )
            for i in range(3)
        ]

        response = ScanResponse(
            scan_id=uuid4(),
            species_id="primary-fish",
            species_name="Primary Fish",
            confidence=0.95,
            alternatives=alternatives,
            scans_remaining=3,
        )

        assert len(response.alternatives) == 3
        assert response.alternatives[0].confidence == 0.9
        assert response.alternatives[1].confidence == 0.8
        assert response.alternatives[2].confidence == 0.7

    def test_json_serialization(self):
        """Test ScanResponse serialization to dict."""
        scan_id = uuid4()
        response = ScanResponse(
            scan_id=scan_id,
            confidence=0.88,
            scans_remaining=2,
        )

        data = response.model_dump()
        assert data["scan_id"] == scan_id
        assert data["confidence"] == 0.88
        assert data["scans_remaining"] == 2
        assert data["alternatives"] == []


class TestScanConfirmRequest:
    """Tests for ScanConfirmRequest schema."""

    def test_valid_confirm_request(self):
        """Test valid ScanConfirmRequest."""
        species_id = "goldfish"
        request = ScanConfirmRequest(species_id=species_id)
        assert request.species_id == species_id

    def test_missing_species_id_rejected(self):
        """Test that missing species_id is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScanConfirmRequest()

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("species_id",)

    def test_empty_string_accepted(self):
        """Test that empty string is accepted (validation at service level)."""
        request = ScanConfirmRequest(species_id="")
        assert request.species_id == ""


class TestScansRemainingResponse:
    """Tests for ScansRemainingResponse schema."""

    def test_free_user_response(self):
        """Test response for free user."""
        response = ScansRemainingResponse(
            scans_remaining=3,
            is_premium=False,
        )
        assert response.scans_remaining == 3
        assert response.is_premium is False

    def test_premium_user_response(self):
        """Test response for premium user."""
        response = ScansRemainingResponse(
            scans_remaining=0,
            is_premium=True,
        )
        assert response.scans_remaining == 0
        assert response.is_premium is True

    def test_zero_scans_remaining(self):
        """Test response with zero scans."""
        response = ScansRemainingResponse(
            scans_remaining=0,
            is_premium=False,
        )
        assert response.scans_remaining == 0

    def test_negative_scans_rejected(self):
        """Test that negative scans_remaining is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScansRemainingResponse(
                scans_remaining=-1,
                is_premium=False,
            )

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("scans_remaining",) for e in errors)

    def test_missing_required_fields_rejected(self):
        """Test that missing required fields are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScansRemainingResponse(scans_remaining=5)

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("is_premium",) for e in errors)

    def test_json_serialization(self):
        """Test ScansRemainingResponse serialization."""
        response = ScansRemainingResponse(
            scans_remaining=5,
            is_premium=False,
        )
        data = response.model_dump()
        assert data["scans_remaining"] == 5
        assert data["is_premium"] is False
