"""Tests for image Pydantic schemas."""

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.image import (
    PresignedUrlItem,
    PresignedUrlResult,
    PresignedUrlsRequest,
    PresignedUrlsResponse,
    UploadResponse,
)


class TestPresignedUrlItem:
    """Tests for PresignedUrlItem schema — request item validation."""

    def test_valid_aquarium(self):
        """Test valid aquarium item creation."""
        item = PresignedUrlItem(
            entity_type="aquarium",
            entity_id=uuid.uuid4(),
        )
        assert item.entity_type == "aquarium"
        assert isinstance(item.entity_id, uuid.UUID)

    def test_valid_fish(self):
        """Test valid fish item creation."""
        item = PresignedUrlItem(entity_type="fish", entity_id=uuid.uuid4())
        assert item.entity_type == "fish"

    def test_valid_avatar(self):
        """Test valid avatar item creation."""
        item = PresignedUrlItem(entity_type="avatar", entity_id=uuid.uuid4())
        assert item.entity_type == "avatar"

    def test_entity_id_from_string(self):
        """Test that entity_id accepts a valid UUID string and parses it."""
        uid = uuid.uuid4()
        item = PresignedUrlItem(entity_type="aquarium", entity_id=str(uid))
        assert item.entity_id == uid

    def test_invalid_entity_type_rejected(self):
        """Test that invalid entity_type is rejected by Literal validation."""
        with pytest.raises(ValidationError) as exc_info:
            PresignedUrlItem(entity_type="unknown", entity_id=uuid.uuid4())

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("entity_type",)

    def test_invalid_entity_id_rejected(self):
        """Test that non-UUID entity_id is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            PresignedUrlItem(entity_type="aquarium", entity_id="not-a-uuid")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("entity_id",)

    def test_empty_entity_type_rejected(self):
        """Test that empty entity_type is rejected."""
        with pytest.raises(ValidationError):
            PresignedUrlItem(entity_type="", entity_id=uuid.uuid4())

    def test_missing_entity_type_rejected(self):
        """Test that missing entity_type is rejected."""
        with pytest.raises(ValidationError):
            PresignedUrlItem(entity_id=uuid.uuid4())  # type: ignore[call-arg]

    def test_missing_entity_id_rejected(self):
        """Test that missing entity_id is rejected."""
        with pytest.raises(ValidationError):
            PresignedUrlItem(entity_type="aquarium")  # type: ignore[call-arg]


class TestPresignedUrlsRequest:
    """Tests for PresignedUrlsRequest schema."""

    def test_valid_request_with_items(self):
        """Test valid request with multiple items."""
        request = PresignedUrlsRequest(
            items=[
                PresignedUrlItem(entity_type="aquarium", entity_id=uuid.uuid4()),
                PresignedUrlItem(entity_type="fish", entity_id=uuid.uuid4()),
                PresignedUrlItem(entity_type="avatar", entity_id=uuid.uuid4()),
            ],
        )
        assert len(request.items) == 3

    def test_valid_request_from_raw_dict(self):
        """Test creating request from raw dict (as FastAPI would receive JSON)."""
        uid = uuid.uuid4()
        request = PresignedUrlsRequest(
            items=[{"entity_type": "aquarium", "entity_id": str(uid)}],
        )
        assert len(request.items) == 1
        assert request.items[0].entity_id == uid

    def test_empty_items_accepted(self):
        """Test that empty items list is accepted at schema level."""
        request = PresignedUrlsRequest(items=[])
        assert len(request.items) == 0

    def test_invalid_item_in_list_rejected(self):
        """Test that a list with an invalid item is rejected."""
        with pytest.raises(ValidationError):
            PresignedUrlsRequest(
                items=[
                    {"entity_type": "aquarium", "entity_id": str(uuid.uuid4())},
                    {"entity_type": "invalid", "entity_id": str(uuid.uuid4())},
                ],
            )


class TestUploadResponse:
    """Tests for UploadResponse schema — serialization."""

    def test_serialization_to_json(self):
        """Test that UploadResponse serializes correctly to JSON-compatible dict."""
        response = UploadResponse(
            key="aquariums/abc/f7a3b2c1.webp",
            entity_type="aquarium",
            entity_id="550e8400-e29b-41d4-a716-446655440000",
        )
        data = response.model_dump()
        assert data["key"] == "aquariums/abc/f7a3b2c1.webp"
        assert data["entity_type"] == "aquarium"
        assert data["entity_id"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_json_output(self):
        """Test model_dump_json produces valid JSON string."""
        response = UploadResponse(
            key="fish/abc/12345678.webp",
            entity_type="fish",
            entity_id="550e8400-e29b-41d4-a716-446655440000",
        )
        json_str = response.model_dump_json()
        assert '"key":"fish/abc/12345678.webp"' in json_str.replace(" ", "")


class TestPresignedUrlResult:
    """Tests for PresignedUrlResult schema."""

    def test_with_photo(self):
        """Test result with photo key and URL."""
        uid = uuid.uuid4()
        result = PresignedUrlResult(
            entity_type="aquarium",
            entity_id=uid,
            key="aquariums/x/y.webp",
            url="https://s3.example.com/presigned",
        )
        assert result.key == "aquariums/x/y.webp"
        assert result.url == "https://s3.example.com/presigned"
        assert result.entity_id == uid

    def test_without_photo(self):
        """Test result with null key and URL (entity has no photo)."""
        result = PresignedUrlResult(
            entity_type="aquarium",
            entity_id=uuid.uuid4(),
            key=None,
            url=None,
        )
        assert result.key is None
        assert result.url is None

    def test_entity_id_serialized_as_string(self):
        """Test that UUID entity_id is serialized as string in JSON."""
        uid = uuid.uuid4()
        result = PresignedUrlResult(
            entity_type="fish",
            entity_id=uid,
            key=None,
            url=None,
        )
        data = result.model_dump(mode="json")
        assert data["entity_id"] == str(uid)


class TestPresignedUrlsResponse:
    """Tests for PresignedUrlsResponse schema."""

    def test_empty_items(self):
        """Test response with empty items list."""
        response = PresignedUrlsResponse(items=[])
        assert len(response.items) == 0

    def test_mixed_items(self):
        """Test response with mixed null and non-null items."""
        response = PresignedUrlsResponse(
            items=[
                PresignedUrlResult(
                    entity_type="aquarium",
                    entity_id=uuid.uuid4(),
                    key="aquariums/x/y.webp",
                    url="https://example.com",
                ),
                PresignedUrlResult(
                    entity_type="fish",
                    entity_id=uuid.uuid4(),
                    key=None,
                    url=None,
                ),
            ],
        )
        assert len(response.items) == 2
        assert response.items[0].key is not None
        assert response.items[1].key is None
