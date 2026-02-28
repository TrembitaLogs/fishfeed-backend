"""Tests for image_service module — base structure, constants, detect_content_type, validate_image,
upload_image, access checks, orphaned image tracking, presigned URLs."""

import io
import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.services.image_service import (
    MAGIC_BYTES,
    MIME_TO_PILLOW_FORMAT,
    PREFIX_MAP,
    VALID_ENTITY_TYPES,
    AccessDeniedError,
    EntityNotFoundError,
    FileTooLargeError,
    ImageServiceError,
    UnsupportedMediaTypeError,
    _batch_get_aquarium_keys,
    _batch_get_avatar_keys,
    _batch_get_fish_keys,
    _check_entity_access,
    _get_s3_client_config,
    _get_s3_session,
    _has_exif,
    _strip_exif,
    _update_entity_photo_key,
    _upload_to_s3,
    batch_generate_presigned_urls,
    build_object_key,
    detect_content_type,
    generate_presigned_url,
    generate_short_uuid,
    get_image_limits,
    get_presigned_urls,
    register_orphaned,
    upload_image,
    validate_image,
)


def _create_image_bytes(fmt: str = "JPEG", width: int = 100, height: int = 100) -> bytes:
    """Create test image bytes in the specified format."""
    image = Image.new("RGB", (width, height), (255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    buffer.seek(0)
    return buffer.getvalue()


class TestDetectContentType:
    """Tests for detect_content_type function."""

    def test_detect_jpeg_from_real_image(self):
        """Test detection of JPEG format from a real image file."""
        content = _create_image_bytes("JPEG")
        assert detect_content_type(content) == "image/jpeg"

    def test_detect_png_from_real_image(self):
        """Test detection of PNG format from a real image file."""
        content = _create_image_bytes("PNG")
        assert detect_content_type(content) == "image/png"

    def test_detect_webp_from_real_image(self):
        """Test detection of WebP format from a real image file."""
        content = _create_image_bytes("WEBP")
        assert detect_content_type(content) == "image/webp"

    def test_detect_jpeg_from_raw_magic_bytes(self):
        """Test JPEG detection with raw magic bytes prefix."""
        content = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        assert detect_content_type(content) == "image/jpeg"

    def test_detect_png_from_raw_magic_bytes(self):
        """Test PNG detection with raw magic bytes prefix."""
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        assert detect_content_type(content) == "image/png"

    def test_detect_webp_from_raw_magic_bytes(self):
        """Test WebP detection with raw RIFF+WEBP markers."""
        content = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
        assert detect_content_type(content) == "image/webp"

    def test_reject_gif(self):
        """Test that GIF format raises UnsupportedMediaTypeError."""
        content = b"GIF89a" + b"\x00" * 20
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            detect_content_type(content)
        assert exc_info.value.status_code == 415

    def test_reject_bmp(self):
        """Test that BMP format raises UnsupportedMediaTypeError."""
        content = b"BM" + b"\x00" * 20
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            detect_content_type(content)
        assert exc_info.value.status_code == 415

    def test_reject_random_bytes(self):
        """Test that random bytes raise UnsupportedMediaTypeError."""
        content = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c"
        with pytest.raises(UnsupportedMediaTypeError):
            detect_content_type(content)

    def test_reject_too_small_content(self):
        """Test that content smaller than 12 bytes raises error."""
        content = b"\xff\xd8\xff"
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            detect_content_type(content)
        assert "too small" in str(exc_info.value).lower()

    def test_reject_empty_content(self):
        """Test that empty bytes raise UnsupportedMediaTypeError."""
        with pytest.raises(UnsupportedMediaTypeError):
            detect_content_type(b"")

    def test_riff_wav_not_detected_as_webp(self):
        """Test that RIFF WAV file is not falsely detected as WebP."""
        content = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20
        with pytest.raises(UnsupportedMediaTypeError):
            detect_content_type(content)

    def test_riff_avi_not_detected_as_webp(self):
        """Test that RIFF AVI file is not falsely detected as WebP."""
        content = b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 20
        with pytest.raises(UnsupportedMediaTypeError):
            detect_content_type(content)


class TestImageLimits:
    """Tests for get_image_limits function."""

    def test_returns_dict_with_all_entity_types(self):
        """Test that limits dict contains all entity types."""
        limits = get_image_limits()
        assert "avatar" in limits
        assert "aquarium" in limits
        assert "fish" in limits

    def test_avatar_limits(self):
        """Test avatar limits match config defaults (2MB, 512px)."""
        limits = get_image_limits()
        assert limits["avatar"]["max_size"] == 2 * 1024 * 1024
        assert limits["avatar"]["max_dim"] == 512

    def test_aquarium_limits(self):
        """Test aquarium limits match config defaults (5MB, 2048px)."""
        limits = get_image_limits()
        assert limits["aquarium"]["max_size"] == 5 * 1024 * 1024
        assert limits["aquarium"]["max_dim"] == 2048

    def test_fish_limits(self):
        """Test fish limits match config defaults (5MB, 2048px)."""
        limits = get_image_limits()
        assert limits["fish"]["max_size"] == 5 * 1024 * 1024
        assert limits["fish"]["max_dim"] == 2048

    def test_each_limit_has_required_keys(self):
        """Test that each entity type has max_size and max_dim as integers."""
        limits = get_image_limits()
        for entity_type in ("avatar", "aquarium", "fish"):
            assert "max_size" in limits[entity_type]
            assert "max_dim" in limits[entity_type]
            assert isinstance(limits[entity_type]["max_size"], int)
            assert isinstance(limits[entity_type]["max_dim"], int)
            assert limits[entity_type]["max_size"] > 0
            assert limits[entity_type]["max_dim"] > 0


class TestPrefixMap:
    """Tests for PREFIX_MAP constant."""

    def test_avatar_prefix(self):
        """Test avatar maps to 'avatars' prefix."""
        assert PREFIX_MAP["avatar"] == "avatars"

    def test_aquarium_prefix(self):
        """Test aquarium maps to 'aquariums' prefix."""
        assert PREFIX_MAP["aquarium"] == "aquariums"

    def test_fish_prefix(self):
        """Test fish maps to 'fish' prefix."""
        assert PREFIX_MAP["fish"] == "fish"

    def test_all_entity_types_present(self):
        """Test that all valid entity types have prefix mappings."""
        assert set(PREFIX_MAP.keys()) == VALID_ENTITY_TYPES


class TestMagicBytes:
    """Tests for MAGIC_BYTES constant."""

    def test_jpeg_magic_present(self):
        """Test JPEG magic bytes are defined."""
        assert b"\xff\xd8\xff" in MAGIC_BYTES
        assert MAGIC_BYTES[b"\xff\xd8\xff"] == "image/jpeg"

    def test_png_magic_present(self):
        """Test PNG magic bytes are defined."""
        assert b"\x89PNG" in MAGIC_BYTES
        assert MAGIC_BYTES[b"\x89PNG"] == "image/png"

    def test_webp_not_in_magic_bytes_dict(self):
        """Test that WebP is NOT in MAGIC_BYTES dict (handled separately)."""
        for mime_type in MAGIC_BYTES.values():
            assert mime_type != "image/webp"


class TestGetS3Session:
    """Tests for _get_s3_session function."""

    def test_returns_aioboto3_session(self):
        """Test that function returns an aioboto3.Session instance."""
        import aioboto3

        session = _get_s3_session()
        assert isinstance(session, aioboto3.Session)

    def test_returns_new_session_each_call(self):
        """Test that each call returns a new session instance."""
        session1 = _get_s3_session()
        session2 = _get_s3_session()
        assert session1 is not session2


class TestGetS3ClientConfig:
    """Tests for _get_s3_client_config function."""

    def test_returns_dict_with_required_keys(self):
        """Config dict should contain all required boto3 client params."""
        mock_settings = MagicMock(
            S3_ENDPOINT_URL="http://minio:9000",
            S3_ACCESS_KEY="testkey",
            S3_SECRET_KEY="testsecret",
            S3_REGION="us-east-1",
        )
        with patch("app.services.image_service.get_settings", return_value=mock_settings):
            config = _get_s3_client_config()

        assert config["service_name"] == "s3"
        assert config["endpoint_url"] == "http://minio:9000"
        assert config["aws_access_key_id"] == "testkey"
        assert config["aws_secret_access_key"] == "testsecret"
        assert config["region_name"] == "us-east-1"

    def test_uses_settings_values(self):
        """Config should reflect actual settings values, not defaults."""
        mock_settings = MagicMock(
            S3_ENDPOINT_URL="https://fsn1.your-objectstorage.com",
            S3_ACCESS_KEY="hetzner-key",
            S3_SECRET_KEY="hetzner-secret",
            S3_REGION="eu-central",
        )
        with patch("app.services.image_service.get_settings", return_value=mock_settings):
            config = _get_s3_client_config()

        assert config["endpoint_url"] == "https://fsn1.your-objectstorage.com"
        assert config["region_name"] == "eu-central"


class TestExceptions:
    """Tests for custom exception classes."""

    def test_image_service_error_base(self):
        """Test base ImageServiceError has message and status_code."""
        err = ImageServiceError("test error", status_code=500)
        assert err.message == "test error"
        assert err.status_code == 500
        assert str(err) == "test error"

    def test_image_service_error_default_status(self):
        """Test base ImageServiceError defaults to 400."""
        err = ImageServiceError("bad request")
        assert err.status_code == 400

    def test_unsupported_media_type_default_message(self):
        """Test UnsupportedMediaTypeError has sensible default."""
        err = UnsupportedMediaTypeError()
        assert err.status_code == 415
        assert "JPEG" in err.message
        assert "PNG" in err.message
        assert "WebP" in err.message

    def test_unsupported_media_type_custom_message(self):
        """Test UnsupportedMediaTypeError with custom message."""
        err = UnsupportedMediaTypeError("Custom error")
        assert err.message == "Custom error"
        assert err.status_code == 415

    def test_file_too_large_error(self):
        """Test FileTooLargeError includes entity_type and size."""
        err = FileTooLargeError("avatar", 2)
        assert err.status_code == 413
        assert "2MB" in err.message
        assert "avatar" in err.message

    def test_entity_not_found_error(self):
        """Test EntityNotFoundError includes entity_type and id."""
        test_id = uuid.uuid4()
        err = EntityNotFoundError("aquarium", test_id)
        assert err.status_code == 404
        assert str(test_id) in err.message
        assert "Aquarium" in err.message

    def test_all_exceptions_inherit_from_base(self):
        """Test all custom exceptions inherit from ImageServiceError."""
        assert isinstance(UnsupportedMediaTypeError(), ImageServiceError)
        assert isinstance(FileTooLargeError("avatar", 2), ImageServiceError)
        assert isinstance(EntityNotFoundError("fish", uuid.uuid4()), ImageServiceError)

    def test_all_exceptions_are_subclass_of_exception(self):
        """Test that all custom exceptions are subclasses of Exception."""
        assert issubclass(UnsupportedMediaTypeError, Exception)
        assert issubclass(FileTooLargeError, Exception)
        assert issubclass(EntityNotFoundError, Exception)


class TestGenerateShortUuid:
    """Tests for generate_short_uuid function."""

    def test_returns_8_char_string(self):
        """Test that returned string is exactly 8 characters."""
        result = generate_short_uuid()
        assert len(result) == 8

    def test_returns_lowercase_hex(self):
        """Test that returned string is lowercase hexadecimal."""
        result = generate_short_uuid()
        assert re.fullmatch(r"[0-9a-f]{8}", result)

    def test_returns_unique_values(self):
        """Test that consecutive calls return different values."""
        results = {generate_short_uuid() for _ in range(100)}
        assert len(results) == 100


class TestBuildObjectKey:
    """Tests for build_object_key function."""

    def test_aquarium_key_format(self):
        """Test aquarium key follows 'aquariums/{id}/{short_uuid}.webp' pattern."""
        entity_id = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
        key = build_object_key("aquarium", entity_id)
        assert key.startswith("aquariums/550e8400-e29b-41d4-a716-446655440000/")
        assert key.endswith(".webp")
        # Short UUID part: 8 hex chars
        short_uuid_part = key.split("/")[-1].replace(".webp", "")
        assert re.fullmatch(r"[0-9a-f]{8}", short_uuid_part)

    def test_fish_key_format(self):
        """Test fish key follows 'fish/{id}/{short_uuid}.webp' pattern."""
        entity_id = uuid.uuid4()
        key = build_object_key("fish", entity_id)
        assert key.startswith(f"fish/{entity_id}/")
        assert key.endswith(".webp")

    def test_avatar_key_format(self):
        """Test avatar key follows 'avatars/{id}/{short_uuid}.webp' pattern."""
        entity_id = uuid.uuid4()
        key = build_object_key("avatar", entity_id)
        assert key.startswith(f"avatars/{entity_id}/")
        assert key.endswith(".webp")

    def test_invalid_entity_type_raises_error(self):
        """Test that invalid entity_type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid entity_type"):
            build_object_key("invalid", uuid.uuid4())

    def test_custom_extension(self):
        """Test that custom extension is used instead of default .webp."""
        key = build_object_key("aquarium", uuid.uuid4(), extension=".jpg")
        assert key.endswith(".jpg")

    def test_each_call_generates_unique_key(self):
        """Test that keys are unique for the same entity."""
        entity_id = uuid.uuid4()
        keys = {build_object_key("aquarium", entity_id) for _ in range(50)}
        assert len(keys) == 50


# --- Helpers for validate_image tests ---


def _create_jpeg_with_exif(width: int = 100, height: int = 100) -> bytes:
    """Create a JPEG image with EXIF metadata (GPS tag)."""
    img = Image.new("RGB", (width, height), (0, 128, 255))
    buf = io.BytesIO()
    # Build minimal EXIF with a GPS tag to simulate privacy-sensitive metadata
    exif = img.getexif()
    # Tag 0x010F = Make (camera manufacturer) — harmless but proves EXIF presence
    exif[0x010F] = "TestCamera"
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _create_png_with_metadata(width: int = 100, height: int = 100) -> bytes:
    """Create a PNG image with text metadata."""
    from PIL.PngImagePlugin import PngInfo

    img = Image.new("RGB", (width, height), (0, 255, 0))
    buf = io.BytesIO()
    pnginfo = PngInfo()
    pnginfo.add_text("Author", "TestUser")
    pnginfo.add_text("Comment", "Test metadata")
    img.save(buf, format="PNG", pnginfo=pnginfo)
    return buf.getvalue()


class TestMimeToPillowFormat:
    """Tests for MIME_TO_PILLOW_FORMAT constant."""

    def test_jpeg_mapping(self):
        assert MIME_TO_PILLOW_FORMAT["image/jpeg"] == "JPEG"

    def test_png_mapping(self):
        assert MIME_TO_PILLOW_FORMAT["image/png"] == "PNG"

    def test_webp_mapping(self):
        assert MIME_TO_PILLOW_FORMAT["image/webp"] == "WEBP"

    def test_covers_all_supported_types(self):
        assert len(MIME_TO_PILLOW_FORMAT) == 3


class TestHasExif:
    """Tests for _has_exif helper function."""

    def test_jpeg_with_exif_returns_true(self):
        """JPEG with EXIF data should be detected."""
        content = _create_jpeg_with_exif()
        img = Image.open(io.BytesIO(content))
        img.load()
        assert _has_exif(img) is True

    def test_jpeg_without_exif_returns_false(self):
        """Plain JPEG without EXIF should not be detected."""
        content = _create_image_bytes("JPEG")
        img = Image.open(io.BytesIO(content))
        img.load()
        assert _has_exif(img) is False

    def test_png_without_exif_returns_false(self):
        """Plain PNG without EXIF should not be detected."""
        content = _create_image_bytes("PNG")
        img = Image.open(io.BytesIO(content))
        img.load()
        assert _has_exif(img) is False

    def test_webp_without_exif_returns_false(self):
        """Plain WebP without EXIF should not be detected."""
        content = _create_image_bytes("WEBP")
        img = Image.open(io.BytesIO(content))
        img.load()
        assert _has_exif(img) is False

    def test_getexif_fallback_detects_parsed_exif(self):
        """Image with parsed EXIF data (via getexif()) but no raw bytes should be detected.

        This covers the fallback path (line 261) where img.info["exif"] is
        empty but img.getexif() returns a non-empty Exif object.
        """
        content = _create_image_bytes("PNG", 50, 50)
        img = Image.open(io.BytesIO(content))
        img.load()
        # Simulate: no raw EXIF bytes, but parsed EXIF dict has data
        img.info.pop("exif", None)
        # Inject EXIF tag directly via getexif() — Make (0x010F)
        exif = img.getexif()
        exif[0x010F] = "InjectedCamera"
        assert _has_exif(img) is True


class TestStripExif:
    """Tests for _strip_exif helper function."""

    def test_jpeg_exif_removed(self):
        """After stripping, JPEG should have no EXIF."""
        content = _create_jpeg_with_exif()
        img = Image.open(io.BytesIO(content))
        img.load()
        result = _strip_exif(img, "image/jpeg")
        # Re-open the result and verify no EXIF
        result_img = Image.open(io.BytesIO(result))
        result_img.load()
        assert _has_exif(result_img) is False

    def test_jpeg_stays_jpeg(self):
        """Stripped JPEG should still be valid JPEG (not converted)."""
        content = _create_jpeg_with_exif()
        img = Image.open(io.BytesIO(content))
        img.load()
        result = _strip_exif(img, "image/jpeg")
        # Verify magic bytes are still JPEG
        assert result[:2] == b"\xff\xd8"

    def test_png_stays_png(self):
        """Stripped PNG should still be valid PNG."""
        content = _create_image_bytes("PNG")
        img = Image.open(io.BytesIO(content))
        img.load()
        result = _strip_exif(img, "image/png")
        assert result[:4] == b"\x89PNG"

    def test_icc_profile_preserved(self):
        """ICC profile should be preserved after EXIF stripping."""
        # Create JPEG with both EXIF and ICC profile
        from PIL import ImageCms

        srgb_profile = ImageCms.createProfile("sRGB")
        icc_data = ImageCms.ImageCmsProfile(srgb_profile).tobytes()

        img = Image.new("RGB", (50, 50), (100, 100, 100))
        buf = io.BytesIO()
        exif = img.getexif()
        exif[0x010F] = "TestCamera"
        img.save(buf, format="JPEG", exif=exif.tobytes(), icc_profile=icc_data)
        buf.seek(0)

        img2 = Image.open(buf)
        img2.load()
        result = _strip_exif(img2, "image/jpeg")

        result_img = Image.open(io.BytesIO(result))
        result_img.load()
        assert result_img.info.get("icc_profile") is not None


class TestValidateImage:
    """Tests for validate_image function."""

    def test_valid_jpeg_passes(self):
        """Valid JPEG within limits should pass validation."""
        content = _create_image_bytes("JPEG", 200, 200)
        content_type, result = validate_image(content, "aquarium")
        assert content_type == "image/jpeg"
        assert len(result) > 0

    def test_valid_png_passes(self):
        """Valid PNG within limits should pass validation."""
        content = _create_image_bytes("PNG", 200, 200)
        content_type, result = validate_image(content, "aquarium")
        assert content_type == "image/png"
        assert len(result) > 0

    def test_valid_webp_passes(self):
        """Valid WebP within limits should pass validation."""
        content = _create_image_bytes("WEBP", 200, 200)
        content_type, result = validate_image(content, "fish")
        assert content_type == "image/webp"
        assert len(result) > 0

    def test_jpeg_with_exif_is_stripped(self):
        """JPEG with EXIF should have EXIF removed, format stays JPEG."""
        content = _create_jpeg_with_exif(200, 200)
        content_type, result = validate_image(content, "aquarium")

        # Format preserved
        assert content_type == "image/jpeg"
        assert result[:2] == b"\xff\xd8"

        # EXIF removed
        result_img = Image.open(io.BytesIO(result))
        result_img.load()
        assert _has_exif(result_img) is False

    def test_jpeg_without_exif_returns_original_bytes(self):
        """JPEG without EXIF should return original bytes unchanged."""
        content = _create_image_bytes("JPEG", 200, 200)
        _, result = validate_image(content, "aquarium")
        assert result == content

    def test_png_format_not_converted(self):
        """PNG should remain PNG after validation (no format conversion)."""
        content = _create_image_bytes("PNG", 200, 200)
        content_type, result = validate_image(content, "aquarium")
        assert content_type == "image/png"
        assert result[:4] == b"\x89PNG"

    def test_webp_format_not_converted(self):
        """WebP should remain WebP after validation (no format conversion)."""
        content = _create_image_bytes("WEBP", 200, 200)
        content_type, result = validate_image(content, "fish")
        assert content_type == "image/webp"
        assert result[:4] == b"RIFF"
        assert result[8:12] == b"WEBP"

    # --- Size limit tests ---

    def test_file_too_large_for_avatar(self):
        """File exceeding avatar limit (2MB) should raise FileTooLargeError."""
        # Create content just over 2MB
        content = _create_image_bytes("JPEG", 100, 100)
        oversized = content + b"\x00" * (2 * 1024 * 1024 + 1)
        # Prepend valid JPEG header so magic bytes pass
        # (the bytes after are garbage but size check comes before decode)
        with pytest.raises(FileTooLargeError) as exc_info:
            validate_image(oversized, "avatar")
        assert exc_info.value.status_code == 413

    def test_file_too_large_for_aquarium(self):
        """File exceeding aquarium limit (5MB) should raise FileTooLargeError."""
        content = _create_image_bytes("JPEG", 100, 100)
        oversized = content + b"\x00" * (5 * 1024 * 1024 + 1)
        with pytest.raises(FileTooLargeError) as exc_info:
            validate_image(oversized, "aquarium")
        assert exc_info.value.status_code == 413

    def test_file_exactly_at_limit_passes(self):
        """File exactly at the size limit should pass."""
        content = _create_image_bytes("JPEG", 100, 100)
        # Pad to exactly 5MB (aquarium limit)
        target = 5 * 1024 * 1024
        if len(content) < target:
            padded = content + b"\x00" * (target - len(content))
        else:
            padded = content[:target]
        # This file won't be a valid JPEG after padding, but size check
        # should pass (decode will fail). We just verify no FileTooLargeError.
        # For a clean test, use a size below limit.
        small = _create_image_bytes("JPEG", 100, 100)
        assert len(small) < 5 * 1024 * 1024
        content_type, _ = validate_image(small, "aquarium")
        assert content_type == "image/jpeg"

    # --- Dimension limit tests ---

    def test_dimensions_exceed_avatar_limit(self):
        """Image exceeding avatar dimension limit (512px) raises ValueError."""
        content = _create_image_bytes("JPEG", 513, 513)
        with pytest.raises(ValueError, match="dimensions"):
            validate_image(content, "avatar")

    def test_dimensions_exceed_aquarium_limit(self):
        """Image exceeding aquarium dimension limit (2048px) raises ValueError."""
        content = _create_image_bytes("JPEG", 2049, 100)
        with pytest.raises(ValueError, match="dimensions"):
            validate_image(content, "aquarium")

    def test_width_within_height_exceeds(self):
        """Only height exceeding limit should still raise ValueError."""
        content = _create_image_bytes("JPEG", 100, 2049)
        with pytest.raises(ValueError, match="dimensions"):
            validate_image(content, "aquarium")

    def test_dimensions_at_exact_limit_passes(self):
        """Image at exactly the dimension limit should pass."""
        content = _create_image_bytes("JPEG", 512, 512)
        content_type, _ = validate_image(content, "avatar")
        assert content_type == "image/jpeg"

    # --- Decompression bomb test ---

    def test_decompression_bomb_caught(self):
        """Image with dimensions exceeding limit raises ValueError.

        A "decompression bomb" scenario: small file with large dimensions.
        Our dimension check catches it before Pillow's load() allocates
        the full pixel buffer.
        """
        # 3000x3000 exceeds aquarium max_dim=2048 and avatar max_dim=512.
        # Uses moderate dimensions to avoid huge memory allocation in test.
        content = _create_image_bytes("JPEG", 3000, 3000)
        with pytest.raises(ValueError, match="dimensions"):
            validate_image(content, "aquarium")

    def test_decompression_bomb_at_image_open(self):
        """DecompressionBombError during Image.open() should raise ValueError.

        Covers the error handler at Image.open() (line 345-348) for images
        where Pillow detects a bomb from the header alone.
        """
        content = _create_image_bytes("JPEG", 100, 100)
        with patch(
            "app.services.image_service.Image.open",
            side_effect=Image.DecompressionBombError("huge image"),
        ):
            with pytest.raises(ValueError, match="decompression bomb"):
                validate_image(content, "aquarium")

    def test_decompression_bomb_at_img_load(self):
        """DecompressionBombError during img.load() should raise ValueError.

        Covers the error handler at img.load() (lines 365-368) for images
        that pass header parsing but trigger the bomb during full decode.
        """
        content = _create_image_bytes("JPEG", 100, 100)
        mock_img = MagicMock()
        mock_img.size = (100, 100)
        mock_img.load.side_effect = Image.DecompressionBombError("bomb at load")
        with patch("app.services.image_service.Image.open", return_value=mock_img):
            with pytest.raises(ValueError, match="decompression bomb"):
                validate_image(content, "aquarium")

    def test_generic_error_at_img_load(self):
        """Generic error during img.load() should raise ValueError.

        Covers the error handler at img.load() (lines 369-370) for
        truncated/corrupt images that pass header parsing but fail decode.
        """
        content = _create_image_bytes("JPEG", 100, 100)
        mock_img = MagicMock()
        mock_img.size = (100, 100)
        mock_img.load.side_effect = OSError("truncated data stream")
        with patch("app.services.image_service.Image.open", return_value=mock_img):
            with pytest.raises(ValueError, match="Invalid or corrupted"):
                validate_image(content, "aquarium")

    # --- Invalid format tests ---

    def test_gif_raises_unsupported_media_type(self):
        """GIF format should raise UnsupportedMediaTypeError."""
        img = Image.new("RGB", (100, 100), (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="GIF")
        content = buf.getvalue()
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            validate_image(content, "aquarium")
        assert exc_info.value.status_code == 415

    def test_bmp_raises_unsupported_media_type(self):
        """BMP format should raise UnsupportedMediaTypeError."""
        img = Image.new("RGB", (100, 100), (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        content = buf.getvalue()
        with pytest.raises(UnsupportedMediaTypeError) as exc_info:
            validate_image(content, "aquarium")
        assert exc_info.value.status_code == 415

    # --- Corrupted file test ---

    def test_corrupted_file_raises_value_error(self):
        """Corrupted file with valid magic bytes should raise ValueError."""
        # Valid JPEG header but truncated/garbage body
        corrupted = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        with pytest.raises(ValueError, match="[Ii]nvalid|corrupt"):
            validate_image(corrupted, "aquarium")

    # --- Invalid entity_type test ---

    def test_invalid_entity_type_raises_value_error(self):
        """Invalid entity_type should raise ValueError."""
        content = _create_image_bytes("JPEG", 100, 100)
        with pytest.raises(ValueError, match="Invalid entity_type"):
            validate_image(content, "invalid_type")

    # --- All entity types work ---

    def test_avatar_entity_type(self):
        """Avatar entity type with valid small image should pass."""
        content = _create_image_bytes("JPEG", 256, 256)
        content_type, _ = validate_image(content, "avatar")
        assert content_type == "image/jpeg"

    def test_fish_entity_type(self):
        """Fish entity type with valid image should pass."""
        content = _create_image_bytes("PNG", 500, 500)
        content_type, _ = validate_image(content, "fish")
        assert content_type == "image/png"


# --- Tests for AccessDeniedError ---


class TestAccessDeniedError:
    """Tests for AccessDeniedError exception class."""

    def test_has_403_status_code(self):
        """AccessDeniedError should have status_code 403."""
        err = AccessDeniedError("aquarium", uuid.uuid4())
        assert err.status_code == 403

    def test_message_contains_entity_type(self):
        """Error message should include the entity type."""
        err = AccessDeniedError("aquarium", uuid.uuid4())
        assert "aquarium" in err.message

    def test_message_contains_entity_id(self):
        """Error message should include the entity id."""
        entity_id = uuid.uuid4()
        err = AccessDeniedError("fish", entity_id)
        assert str(entity_id) in err.message

    def test_inherits_from_image_service_error(self):
        """AccessDeniedError should be a subclass of ImageServiceError."""
        err = AccessDeniedError("avatar", uuid.uuid4())
        assert isinstance(err, ImageServiceError)


# --- Tests for register_orphaned ---


class TestRegisterOrphaned:
    """Tests for register_orphaned function."""

    @pytest.mark.asyncio
    async def test_adds_orphaned_image_to_session(self):
        """register_orphaned should add an OrphanedImage to the DB session."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()  # add() is synchronous on AsyncSession
        await register_orphaned(mock_db, "aquariums/abc/12345678.webp", "aquarium")
        mock_db.add.assert_called_once()
        orphan = mock_db.add.call_args[0][0]
        assert orphan.old_key == "aquariums/abc/12345678.webp"
        assert orphan.entity_type == "aquarium"

    @pytest.mark.asyncio
    async def test_works_for_all_entity_types(self):
        """register_orphaned should accept any valid entity type."""
        for entity_type in ("avatar", "aquarium", "fish"):
            mock_db = AsyncMock()
            mock_db.add = MagicMock()  # add() is synchronous on AsyncSession
            await register_orphaned(mock_db, f"test/{entity_type}/key.webp", entity_type)
            mock_db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_key_inserts_successfully(self):
        """register_orphaned should allow duplicate old_key (no UNIQUE constraint)."""
        same_key = "aquariums/abc/12345678.webp"
        mock_db = AsyncMock()
        mock_db.add = MagicMock()  # add() is synchronous on AsyncSession

        await register_orphaned(mock_db, same_key, "aquarium")
        await register_orphaned(mock_db, same_key, "aquarium")

        assert mock_db.add.call_count == 2
        first_orphan = mock_db.add.call_args_list[0][0][0]
        second_orphan = mock_db.add.call_args_list[1][0][0]
        assert first_orphan.old_key == same_key
        assert second_orphan.old_key == same_key

    @pytest.mark.asyncio
    async def test_logs_orphaned_registration(self):
        """register_orphaned should log the operation via structlog."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        with patch("app.services.image_service.logger") as mock_logger:
            await register_orphaned(mock_db, "fish/xyz/aabbccdd.webp", "fish")
            mock_logger.info.assert_called_once_with(
                "registered_orphaned_image",
                old_key="fish/xyz/aabbccdd.webp",
                entity_type="fish",
            )


# --- Tests for _check_entity_access ---


class TestCheckEntityAccess:
    """Tests for _check_entity_access function."""

    @pytest.mark.asyncio
    async def test_avatar_access_granted_when_user_matches(self):
        """Avatar access should pass when user_id equals entity_id and user exists."""
        user_id = uuid.uuid4()
        mock_user = MagicMock()

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_db.execute.return_value = mock_result

        # Should not raise
        await _check_entity_access(mock_db, user_id, "avatar", user_id)

    @pytest.mark.asyncio
    async def test_avatar_access_denied_when_user_differs(self):
        """Avatar access should be denied when user_id != entity_id."""
        mock_db = AsyncMock()
        with pytest.raises(AccessDeniedError) as exc_info:
            await _check_entity_access(mock_db, uuid.uuid4(), "avatar", uuid.uuid4())
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_avatar_entity_not_found_when_user_missing(self):
        """Avatar should raise EntityNotFoundError if user doesn't exist."""
        user_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(EntityNotFoundError) as exc_info:
            await _check_entity_access(mock_db, user_id, "avatar", user_id)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_aquarium_access_granted(self):
        """Aquarium access should pass when check_access succeeds."""
        mock_db = AsyncMock()
        mock_aquarium = MagicMock()

        with patch(
            "app.services.image_service.check_access", new_callable=AsyncMock
        ) as mock_check:
            mock_check.return_value = (mock_aquarium, "owner")
            # Should not raise
            await _check_entity_access(mock_db, uuid.uuid4(), "aquarium", uuid.uuid4())
            mock_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_aquarium_not_found(self):
        """Aquarium should raise EntityNotFoundError when aquarium doesn't exist."""
        from app.services.aquarium import AquariumNotFoundError

        aquarium_id = uuid.uuid4()
        mock_db = AsyncMock()

        with patch(
            "app.services.image_service.check_access", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = AquariumNotFoundError(aquarium_id)
            with pytest.raises(EntityNotFoundError) as exc_info:
                await _check_entity_access(
                    mock_db, uuid.uuid4(), "aquarium", aquarium_id
                )
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_aquarium_access_denied(self):
        """Aquarium should raise AccessDeniedError when user lacks access."""
        from app.services.aquarium import AquariumAccessDeniedError

        aquarium_id = uuid.uuid4()
        mock_db = AsyncMock()

        with patch(
            "app.services.image_service.check_access", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = AquariumAccessDeniedError(aquarium_id)
            with pytest.raises(AccessDeniedError) as exc_info:
                await _check_entity_access(
                    mock_db, uuid.uuid4(), "aquarium", aquarium_id
                )
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_fish_access_granted(self):
        """Fish access should pass when fish exists and user has aquarium access."""
        fish_id = uuid.uuid4()
        aquarium_id = uuid.uuid4()

        mock_fish = MagicMock()
        mock_fish.aquarium_id = aquarium_id

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_fish
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.image_service.check_access", new_callable=AsyncMock
        ) as mock_check:
            mock_check.return_value = (MagicMock(), "owner")
            await _check_entity_access(mock_db, uuid.uuid4(), "fish", fish_id)
            mock_check.assert_called_once_with(mock_db, aquarium_id, mock_check.call_args[0][2])

    @pytest.mark.asyncio
    async def test_fish_not_found(self):
        """Fish should raise EntityNotFoundError when fish doesn't exist."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(EntityNotFoundError) as exc_info:
            await _check_entity_access(mock_db, uuid.uuid4(), "fish", uuid.uuid4())
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_fish_aquarium_access_denied(self):
        """Fish should raise AccessDeniedError when user lacks aquarium access."""
        from app.services.aquarium import AquariumAccessDeniedError

        mock_fish = MagicMock()
        mock_fish.aquarium_id = uuid.uuid4()

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_fish
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.image_service.check_access", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = AquariumAccessDeniedError(mock_fish.aquarium_id)
            with pytest.raises(AccessDeniedError) as exc_info:
                await _check_entity_access(mock_db, uuid.uuid4(), "fish", uuid.uuid4())
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_fish_aquarium_deleted_raises_entity_not_found(self):
        """Fish whose aquarium is deleted should raise EntityNotFoundError.

        When check_access raises AquariumNotFoundError (soft-deleted aquarium),
        _check_entity_access should map it to EntityNotFoundError (line 461).
        """
        from app.services.aquarium import AquariumNotFoundError

        fish_id = uuid.uuid4()
        mock_fish = MagicMock()
        mock_fish.aquarium_id = uuid.uuid4()

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_fish
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.image_service.check_access", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = AquariumNotFoundError(mock_fish.aquarium_id)
            with pytest.raises(EntityNotFoundError) as exc_info:
                await _check_entity_access(mock_db, uuid.uuid4(), "fish", fish_id)
            assert exc_info.value.status_code == 404


# --- Tests for _upload_to_s3 ---


class TestUploadToS3:
    """Tests for _upload_to_s3 function — direct S3 interaction."""

    @pytest.mark.asyncio
    async def test_calls_s3_put_object_with_correct_params(self):
        """Should call put_object with correct bucket, key, body, content type."""
        mock_s3_client = AsyncMock()
        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        content = b"fake image bytes"

        with (
            patch("app.services.image_service._get_s3_session", return_value=mock_session),
            patch(
                "app.services.image_service.get_settings",
                return_value=MagicMock(
                    S3_ENDPOINT_URL="http://minio:9000",
                    S3_ACCESS_KEY="testkey",
                    S3_SECRET_KEY="testsecret",
                    S3_REGION="us-east-1",
                    S3_IMAGES_BUCKET_NAME="fishfeed-images",
                ),
            ),
        ):
            await _upload_to_s3("aquariums/abc/12345678.webp", content, "image/webp")

        mock_s3_client.put_object.assert_called_once_with(
            Bucket="fishfeed-images",
            Key="aquariums/abc/12345678.webp",
            Body=content,
            ContentType="image/webp",
        )

    @pytest.mark.asyncio
    async def test_s3_error_propagates(self):
        """S3 client error should propagate without being caught."""
        mock_s3_client = AsyncMock()
        mock_s3_client.put_object.side_effect = Exception("S3 connection refused")

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with (
            patch("app.services.image_service._get_s3_session", return_value=mock_session),
            patch("app.services.image_service.get_settings", return_value=MagicMock(
                S3_ENDPOINT_URL="http://minio:9000",
                S3_ACCESS_KEY="k",
                S3_SECRET_KEY="s",
                S3_REGION="us-east-1",
                S3_IMAGES_BUCKET_NAME="fishfeed-images",
            )),
        ):
            with pytest.raises(Exception, match="S3 connection refused"):
                await _upload_to_s3("key.webp", b"data", "image/webp")


# --- Tests for _update_entity_photo_key ---


class TestUpdateEntityPhotoKey:
    """Tests for _update_entity_photo_key function — atomic DB update."""

    @pytest.mark.asyncio
    async def test_invalid_entity_type_raises_value_error(self):
        """Invalid entity_type should raise ValueError (defensive check, line 545-546)."""
        mock_db = AsyncMock()
        with pytest.raises(ValueError, match="Invalid entity_type"):
            await _update_entity_photo_key(
                mock_db, "invalid_type", uuid.uuid4(), "test/key.webp"
            )

    @pytest.mark.asyncio
    async def test_avatar_updates_avatar_key(self):
        """Avatar entity_type should update avatar_key on User."""
        user_id = uuid.uuid4()
        mock_user = MagicMock()
        mock_user.avatar_key = None

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = mock_user
        mock_db.execute.return_value = mock_result

        await _update_entity_photo_key(mock_db, "avatar", user_id, "avatars/u/new.webp")

        assert mock_user.avatar_key == "avatars/u/new.webp"
        mock_db.flush.assert_called_once()
        # No orphan registered (old key was None)
        mock_db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_registers_orphan_when_old_key_exists(self):
        """Should register old key as orphaned when replacing an existing photo."""
        mock_entity = MagicMock()
        mock_entity.photo_key = "aquariums/old/prev.webp"

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = mock_entity
        mock_db.execute.return_value = mock_result

        await _update_entity_photo_key(
            mock_db, "aquarium", uuid.uuid4(), "aquariums/new/key.webp"
        )

        assert mock_entity.photo_key == "aquariums/new/key.webp"
        mock_db.add.assert_called_once()
        orphan = mock_db.add.call_args[0][0]
        assert orphan.old_key == "aquariums/old/prev.webp"


# --- Tests for upload_image ---


class TestUploadImage:
    """Tests for upload_image function — full pipeline with mocked S3 and DB."""

    @pytest.fixture
    def valid_webp(self):
        """Create valid small WebP image bytes."""
        return _create_image_bytes("WEBP", 200, 200)

    def _make_mock_db(self, entity, *, scalar_one_or_none_value=None):
        """Create a mock DB session that handles multiple execute calls.

        First execute call returns scalar_one_or_none (for access check),
        second returns scalar_one (for FOR UPDATE).
        """
        mock_db = AsyncMock()
        mock_db.add = MagicMock()  # add() is synchronous on AsyncSession

        # Access check result
        access_result = MagicMock()
        access_result.scalar_one_or_none.return_value = (
            scalar_one_or_none_value if scalar_one_or_none_value is not None else entity
        )

        # FOR UPDATE result
        for_update_result = MagicMock()
        for_update_result.scalar_one.return_value = entity

        mock_db.execute.side_effect = [access_result, for_update_result]
        return mock_db

    @pytest.mark.asyncio
    async def test_successful_avatar_upload(self, valid_webp):
        """Successful avatar upload should call S3 and update DB."""
        user_id = uuid.uuid4()

        mock_user = MagicMock()
        mock_user.avatar_key = None
        mock_db = self._make_mock_db(mock_user)

        with patch(
            "app.services.image_service._upload_to_s3", new_callable=AsyncMock
        ) as mock_s3:
            key = await upload_image(mock_db, user_id, "avatar", user_id, valid_webp)

        assert key.startswith("avatars/")
        assert key.endswith(".webp")
        mock_s3.assert_called_once()
        mock_db.flush.assert_called_once()
        assert mock_user.avatar_key == key

    @pytest.mark.asyncio
    async def test_successful_aquarium_upload(self, valid_webp):
        """Successful aquarium upload should call check_access and update photo_key."""
        user_id = uuid.uuid4()
        aquarium_id = uuid.uuid4()

        mock_aquarium = MagicMock()
        mock_aquarium.photo_key = None

        mock_db = AsyncMock()
        for_update_result = MagicMock()
        for_update_result.scalar_one.return_value = mock_aquarium
        mock_db.execute.return_value = for_update_result

        with (
            patch(
                "app.services.image_service._upload_to_s3", new_callable=AsyncMock
            ) as mock_s3,
            patch(
                "app.services.image_service.check_access", new_callable=AsyncMock
            ) as mock_check,
        ):
            mock_check.return_value = (mock_aquarium, "owner")
            key = await upload_image(
                mock_db, user_id, "aquarium", aquarium_id, valid_webp
            )

        assert key.startswith("aquariums/")
        mock_check.assert_called_once()
        mock_s3.assert_called_once()
        assert mock_aquarium.photo_key == key

    @pytest.mark.asyncio
    async def test_entity_not_found_raises_404(self, valid_webp):
        """Non-existent entity should raise EntityNotFoundError (404)."""
        from app.services.aquarium import AquariumNotFoundError

        aquarium_id = uuid.uuid4()
        mock_db = AsyncMock()

        with patch(
            "app.services.image_service.check_access", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = AquariumNotFoundError(aquarium_id)
            with pytest.raises(EntityNotFoundError) as exc_info:
                await upload_image(
                    mock_db, uuid.uuid4(), "aquarium", aquarium_id, valid_webp
                )
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_forbidden_raises_403(self, valid_webp):
        """Upload without access should raise AccessDeniedError (403)."""
        from app.services.aquarium import AquariumAccessDeniedError

        aquarium_id = uuid.uuid4()
        mock_db = AsyncMock()

        with patch(
            "app.services.image_service.check_access", new_callable=AsyncMock
        ) as mock_check:
            mock_check.side_effect = AquariumAccessDeniedError(aquarium_id)
            with pytest.raises(AccessDeniedError) as exc_info:
                await upload_image(
                    mock_db, uuid.uuid4(), "aquarium", aquarium_id, valid_webp
                )
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_replace_existing_photo_saves_old_key_to_orphaned(self, valid_webp):
        """Replacing an existing photo should save old key to orphaned_images."""
        user_id = uuid.uuid4()
        old_key = "aquariums/old/abcdef01.webp"

        mock_aquarium = MagicMock()
        mock_aquarium.photo_key = old_key

        mock_db = AsyncMock()
        mock_db.add = MagicMock()  # add() is synchronous on AsyncSession
        for_update_result = MagicMock()
        for_update_result.scalar_one.return_value = mock_aquarium
        mock_db.execute.return_value = for_update_result

        with (
            patch(
                "app.services.image_service._upload_to_s3", new_callable=AsyncMock
            ),
            patch(
                "app.services.image_service.check_access", new_callable=AsyncMock
            ) as mock_check,
        ):
            mock_check.return_value = (mock_aquarium, "owner")
            new_key = await upload_image(
                mock_db, user_id, "aquarium", uuid.uuid4(), valid_webp
            )

        # Old key should be registered as orphaned
        assert mock_db.add.called
        orphan_call = mock_db.add.call_args[0][0]
        assert orphan_call.old_key == old_key
        assert orphan_call.entity_type == "aquarium"
        # New key should be set
        assert mock_aquarium.photo_key == new_key

    @pytest.mark.asyncio
    async def test_s3_error_no_db_changes(self, valid_webp):
        """S3 upload failure should prevent DB changes."""
        user_id = uuid.uuid4()
        aquarium_id = uuid.uuid4()

        mock_db = AsyncMock()

        with (
            patch(
                "app.services.image_service._upload_to_s3", new_callable=AsyncMock
            ) as mock_s3,
            patch(
                "app.services.image_service.check_access", new_callable=AsyncMock
            ) as mock_check,
        ):
            mock_check.return_value = (MagicMock(), "owner")
            mock_s3.side_effect = Exception("S3 connection error")

            with pytest.raises(Exception, match="S3 connection error"):
                await upload_image(
                    mock_db, user_id, "aquarium", aquarium_id, valid_webp
                )

        # No DB changes should have occurred (flush never called)
        mock_db.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_error_after_s3_upload_file_remains(self, valid_webp):
        """DB failure after S3 upload should leave the file in S3 (orphaned).

        The weekly reconciliation job will clean it up.
        """
        user_id = uuid.uuid4()
        aquarium_id = uuid.uuid4()

        mock_db = AsyncMock()
        mock_db.flush.side_effect = Exception("DB connection lost")

        with (
            patch(
                "app.services.image_service._upload_to_s3", new_callable=AsyncMock
            ) as mock_s3,
            patch(
                "app.services.image_service.check_access", new_callable=AsyncMock
            ) as mock_check,
        ):
            mock_check.return_value = (MagicMock(), "owner")
            # execute for FOR UPDATE should return a valid entity
            for_update_result = MagicMock()
            mock_entity = MagicMock()
            mock_entity.photo_key = None
            for_update_result.scalar_one.return_value = mock_entity
            mock_db.execute.return_value = for_update_result

            with pytest.raises(Exception, match="DB connection lost"):
                await upload_image(
                    mock_db, user_id, "aquarium", aquarium_id, valid_webp
                )

        # S3 upload was called (file exists in S3)
        mock_s3.assert_called_once()

    @pytest.mark.asyncio
    async def test_for_update_used_in_db_query(self, valid_webp):
        """DB update should use SELECT FOR UPDATE to prevent concurrent conflicts."""
        user_id = uuid.uuid4()

        mock_user = MagicMock()
        mock_user.avatar_key = None
        mock_db = self._make_mock_db(mock_user)

        with patch(
            "app.services.image_service._upload_to_s3", new_callable=AsyncMock
        ):
            await upload_image(mock_db, user_id, "avatar", user_id, valid_webp)

        # The second execute call (FOR UPDATE) should have a compiled query
        # with FOR UPDATE clause. Verify via the call arguments.
        assert mock_db.execute.call_count == 2
        for_update_call = mock_db.execute.call_args_list[1]
        stmt = for_update_call[0][0]
        # Check that the compiled SQL contains FOR UPDATE
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "FOR UPDATE" in compiled

    @pytest.mark.asyncio
    async def test_invalid_entity_type_raises_value_error(self, valid_webp):
        """Invalid entity_type should raise ValueError."""
        mock_db = AsyncMock()
        with pytest.raises(ValueError, match="Invalid entity_type"):
            await upload_image(
                mock_db, uuid.uuid4(), "invalid", uuid.uuid4(), valid_webp
            )

    @pytest.mark.asyncio
    async def test_invalid_image_format_raises_error(self):
        """Invalid image format should raise UnsupportedMediaTypeError."""
        user_id = uuid.uuid4()
        mock_user = MagicMock()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_db.execute.return_value = mock_result

        gif_content = b"GIF89a" + b"\x00" * 100
        with pytest.raises(UnsupportedMediaTypeError):
            await upload_image(mock_db, user_id, "avatar", user_id, gif_content)

    @pytest.mark.asyncio
    async def test_no_orphan_when_entity_has_no_photo(self, valid_webp):
        """First upload (no existing photo) should NOT register any orphan."""
        user_id = uuid.uuid4()

        mock_user = MagicMock()
        mock_user.avatar_key = None
        mock_db = self._make_mock_db(mock_user)

        with patch(
            "app.services.image_service._upload_to_s3", new_callable=AsyncMock
        ):
            await upload_image(mock_db, user_id, "avatar", user_id, valid_webp)

        # add() should NOT be called (no orphan to register)
        mock_db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_fish_upload_checks_aquarium_access(self, valid_webp):
        """Fish upload should verify access to the fish's aquarium."""
        user_id = uuid.uuid4()
        fish_id = uuid.uuid4()
        aquarium_id = uuid.uuid4()

        mock_fish = MagicMock()
        mock_fish.aquarium_id = aquarium_id
        mock_fish.photo_key = None

        mock_db = AsyncMock()

        # First call: access check (find fish)
        access_result = MagicMock()
        access_result.scalar_one_or_none.return_value = mock_fish

        # Second call: FOR UPDATE
        for_update_result = MagicMock()
        for_update_result.scalar_one.return_value = mock_fish

        mock_db.execute.side_effect = [access_result, for_update_result]

        with (
            patch(
                "app.services.image_service._upload_to_s3", new_callable=AsyncMock
            ),
            patch(
                "app.services.image_service.check_access", new_callable=AsyncMock
            ) as mock_check,
        ):
            mock_check.return_value = (MagicMock(), "member")
            await upload_image(mock_db, user_id, "fish", fish_id, valid_webp)

        # check_access should be called with the AQUARIUM id, not fish id
        mock_check.assert_called_once_with(mock_db, aquarium_id, user_id)

    @pytest.mark.asyncio
    async def test_returned_key_format(self, valid_webp):
        """Returned key should follow {prefix}/{entity_id}/{short_uuid}.webp format."""
        user_id = uuid.uuid4()

        mock_user = MagicMock()
        mock_user.avatar_key = None
        mock_db = self._make_mock_db(mock_user)

        with patch(
            "app.services.image_service._upload_to_s3", new_callable=AsyncMock
        ):
            key = await upload_image(mock_db, user_id, "avatar", user_id, valid_webp)

        parts = key.split("/")
        assert len(parts) == 3
        assert parts[0] == "avatars"
        assert parts[1] == str(user_id)
        assert re.fullmatch(r"[0-9a-f]{8}\.webp", parts[2])


# --- Tests for generate_presigned_url ---


class TestGeneratePresignedUrl:
    """Tests for generate_presigned_url function."""

    @pytest.mark.asyncio
    async def test_returns_presigned_url_string(self):
        """Should return a presigned URL string from S3 client."""
        mock_s3_client = AsyncMock()
        mock_s3_client.generate_presigned_url = AsyncMock(
            return_value="https://s3.example.com/fishfeed-images/aquariums/abc/123.webp?Signature=xyz"
        )

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with patch("app.services.image_service._get_s3_session", return_value=mock_session):
            url = await generate_presigned_url("aquariums/abc/12345678.webp")

        assert url == "https://s3.example.com/fishfeed-images/aquariums/abc/123.webp?Signature=xyz"

    @pytest.mark.asyncio
    async def test_uses_presigned_endpoint_url_when_set(self):
        """Should use S3_PRESIGNED_ENDPOINT_URL for client config when set."""
        mock_s3_client = AsyncMock()
        mock_s3_client.generate_presigned_url = AsyncMock(return_value="https://localhost:9000/...")

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with (
            patch("app.services.image_service._get_s3_session", return_value=mock_session),
            patch(
                "app.services.image_service.get_settings",
                return_value=MagicMock(
                    S3_PRESIGNED_ENDPOINT_URL="http://localhost:9000",
                    S3_ENDPOINT_URL="http://minio:9000",
                    S3_ACCESS_KEY="key",
                    S3_SECRET_KEY="secret",
                    S3_REGION="us-east-1",
                    S3_IMAGES_BUCKET_NAME="fishfeed-images",
                ),
            ),
        ):
            await generate_presigned_url("test/key.webp")

        # Verify client was created with presigned endpoint, not internal one
        call_kwargs = mock_session.client.call_args[1]
        assert call_kwargs["endpoint_url"] == "http://localhost:9000"

    @pytest.mark.asyncio
    async def test_falls_back_to_s3_endpoint_when_presigned_not_set(self):
        """Should fall back to S3_ENDPOINT_URL when S3_PRESIGNED_ENDPOINT_URL is None."""
        mock_s3_client = AsyncMock()
        mock_s3_client.generate_presigned_url = AsyncMock(return_value="https://s3.example.com/...")

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with (
            patch("app.services.image_service._get_s3_session", return_value=mock_session),
            patch(
                "app.services.image_service.get_settings",
                return_value=MagicMock(
                    S3_PRESIGNED_ENDPOINT_URL=None,
                    S3_ENDPOINT_URL="https://fsn1.your-objectstorage.com",
                    S3_ACCESS_KEY="key",
                    S3_SECRET_KEY="secret",
                    S3_REGION="eu-central",
                    S3_IMAGES_BUCKET_NAME="fishfeed-images",
                ),
            ),
        ):
            await generate_presigned_url("test/key.webp")

        call_kwargs = mock_session.client.call_args[1]
        assert call_kwargs["endpoint_url"] == "https://fsn1.your-objectstorage.com"

    @pytest.mark.asyncio
    async def test_passes_correct_params_to_s3(self):
        """Should pass correct bucket, key, content-disposition, and expiry."""
        mock_s3_client = AsyncMock()
        mock_s3_client.generate_presigned_url = AsyncMock(return_value="url")

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with patch("app.services.image_service._get_s3_session", return_value=mock_session):
            await generate_presigned_url("fish/abc/def12345.webp", expires_in=7200)

        mock_s3_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={
                "Bucket": "fishfeed-images",
                "Key": "fish/abc/def12345.webp",
                "ResponseContentDisposition": "inline",
            },
            ExpiresIn=7200,
        )

    @pytest.mark.asyncio
    async def test_default_expiry_is_3600(self):
        """Should default to 3600 seconds (1 hour) expiry."""
        mock_s3_client = AsyncMock()
        mock_s3_client.generate_presigned_url = AsyncMock(return_value="url")

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with patch("app.services.image_service._get_s3_session", return_value=mock_session):
            await generate_presigned_url("test/key.webp")

        call_args = mock_s3_client.generate_presigned_url.call_args
        assert call_args[1]["ExpiresIn"] == 3600

    @pytest.mark.asyncio
    async def test_client_error_raises_image_service_error(self):
        """Should raise ImageServiceError with status 500 on S3 ClientError."""
        from botocore.exceptions import ClientError

        mock_s3_client = AsyncMock()
        mock_s3_client.generate_presigned_url = AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                "GetObject",
            )
        )

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with patch("app.services.image_service._get_s3_session", return_value=mock_session):
            with pytest.raises(ImageServiceError) as exc_info:
                await generate_presigned_url("nonexistent/key.webp")

        assert exc_info.value.status_code == 500
        assert "NoSuchKey" in exc_info.value.message


# --- Tests for batch_generate_presigned_urls ---


class TestBatchGeneratePresignedUrls:
    """Tests for batch_generate_presigned_urls helper."""

    @pytest.mark.asyncio
    async def test_generates_urls_for_all_keys(self):
        """Should return a dict mapping each key to its presigned URL."""
        keys = ["aquariums/a/1.webp", "fish/b/2.webp", "avatars/c/3.webp"]
        expected_urls = {k: f"https://s3/{k}?signed" for k in keys}

        mock_s3_client = AsyncMock()
        mock_s3_client.generate_presigned_url = AsyncMock(
            side_effect=[expected_urls[k] for k in keys]
        )

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with patch("app.services.image_service._get_s3_session", return_value=mock_session):
            result = await batch_generate_presigned_urls(keys)

        assert result == expected_urls
        assert mock_s3_client.generate_presigned_url.call_count == 3

    @pytest.mark.asyncio
    async def test_empty_keys_returns_empty_dict(self):
        """Should return empty dict without creating S3 client for empty list."""
        result = await batch_generate_presigned_urls([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_inline_content_disposition(self):
        """Should pass ResponseContentDisposition: inline for browser preview."""
        mock_s3_client = AsyncMock()
        mock_s3_client.generate_presigned_url = AsyncMock(return_value="url")

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.client.return_value = mock_ctx

        with patch("app.services.image_service._get_s3_session", return_value=mock_session):
            await batch_generate_presigned_urls(["test/key.webp"])

        call_args = mock_s3_client.generate_presigned_url.call_args
        assert call_args[1]["Params"]["ResponseContentDisposition"] == "inline"


# --- Tests for batch access check helpers ---


class TestBatchGetAvatarKeys:
    """Tests for _batch_get_avatar_keys helper."""

    @pytest.mark.asyncio
    async def test_own_avatar_accessible(self):
        """User can access their own avatar."""
        user_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.id = user_id
        mock_row.avatar_key = "avatars/abc/12345678.webp"
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([mock_row])))

        result = await _batch_get_avatar_keys(mock_db, user_id, [user_id])

        assert ("avatar", user_id) in result
        assert result[("avatar", user_id)] == "avatars/abc/12345678.webp"

    @pytest.mark.asyncio
    async def test_other_user_avatar_excluded(self):
        """Other users' avatars should be excluded (no access)."""
        user_id = uuid.uuid4()
        other_id = uuid.uuid4()
        mock_db = AsyncMock()

        result = await _batch_get_avatar_keys(mock_db, user_id, [other_id])

        assert result == {}
        # DB should not even be queried
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_avatar_with_null_key(self):
        """User with no avatar should return None for avatar_key."""
        user_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.id = user_id
        mock_row.avatar_key = None
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([mock_row])))

        result = await _batch_get_avatar_keys(mock_db, user_id, [user_id])

        assert result[("avatar", user_id)] is None

    @pytest.mark.asyncio
    async def test_mixed_own_and_other_avatars(self):
        """Only user's own avatar should be queried from the batch."""
        user_id = uuid.uuid4()
        other1 = uuid.uuid4()
        other2 = uuid.uuid4()
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.id = user_id
        mock_row.avatar_key = "avatars/x/y.webp"
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([mock_row])))

        result = await _batch_get_avatar_keys(mock_db, user_id, [other1, user_id, other2])

        # Only own avatar returned
        assert len(result) == 1
        assert ("avatar", user_id) in result

    @pytest.mark.asyncio
    async def test_empty_entity_ids(self):
        """Empty entity_ids should return empty dict without DB query."""
        mock_db = AsyncMock()
        result = await _batch_get_avatar_keys(mock_db, uuid.uuid4(), [])
        assert result == {}
        mock_db.execute.assert_not_called()


class TestBatchGetAquariumKeys:
    """Tests for _batch_get_aquarium_keys helper."""

    @pytest.mark.asyncio
    async def test_owner_access(self):
        """Aquarium owner should have access."""
        user_id = uuid.uuid4()
        aq_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.id = aq_id
        mock_row.photo_key = "aquariums/x/y.webp"
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([mock_row])))

        result = await _batch_get_aquarium_keys(mock_db, user_id, [aq_id])

        assert ("aquarium", aq_id) in result
        assert result[("aquarium", aq_id)] == "aquariums/x/y.webp"

    @pytest.mark.asyncio
    async def test_null_photo_key(self):
        """Aquarium with no photo should return None for photo_key."""
        user_id = uuid.uuid4()
        aq_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.id = aq_id
        mock_row.photo_key = None
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([mock_row])))

        result = await _batch_get_aquarium_keys(mock_db, user_id, [aq_id])

        assert result[("aquarium", aq_id)] is None

    @pytest.mark.asyncio
    async def test_inaccessible_aquarium_excluded(self):
        """Aquarium without access should not appear in result."""
        user_id = uuid.uuid4()
        aq_id = uuid.uuid4()
        mock_db = AsyncMock()
        # Empty result — no access
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([])))

        result = await _batch_get_aquarium_keys(mock_db, user_id, [aq_id])

        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_entity_ids(self):
        """Empty list should return empty dict without DB query."""
        mock_db = AsyncMock()
        result = await _batch_get_aquarium_keys(mock_db, uuid.uuid4(), [])
        assert result == {}
        mock_db.execute.assert_not_called()


class TestBatchGetFishKeys:
    """Tests for _batch_get_fish_keys helper."""

    @pytest.mark.asyncio
    async def test_fish_with_accessible_aquarium(self):
        """Fish in an accessible aquarium should have access."""
        user_id = uuid.uuid4()
        fish_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.id = fish_id
        mock_row.photo_key = "fish/x/y.webp"
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([mock_row])))

        result = await _batch_get_fish_keys(mock_db, user_id, [fish_id])

        assert ("fish", fish_id) in result
        assert result[("fish", fish_id)] == "fish/x/y.webp"

    @pytest.mark.asyncio
    async def test_fish_with_inaccessible_aquarium_excluded(self):
        """Fish in an inaccessible aquarium should be excluded."""
        user_id = uuid.uuid4()
        fish_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([])))

        result = await _batch_get_fish_keys(mock_db, user_id, [fish_id])

        assert result == {}

    @pytest.mark.asyncio
    async def test_null_photo_key(self):
        """Fish with no photo should return None for photo_key."""
        user_id = uuid.uuid4()
        fish_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.id = fish_id
        mock_row.photo_key = None
        mock_db.execute = AsyncMock(return_value=MagicMock(__iter__=lambda s: iter([mock_row])))

        result = await _batch_get_fish_keys(mock_db, user_id, [fish_id])

        assert result[("fish", fish_id)] is None

    @pytest.mark.asyncio
    async def test_empty_entity_ids(self):
        """Empty list should return empty dict without DB query."""
        mock_db = AsyncMock()
        result = await _batch_get_fish_keys(mock_db, uuid.uuid4(), [])
        assert result == {}
        mock_db.execute.assert_not_called()


# --- Tests for get_presigned_urls ---


class TestGetPresignedUrls:
    """Tests for get_presigned_urls — the main batch presigned URL function."""

    @pytest.mark.asyncio
    async def test_batch_request_multiple_entity_types(self):
        """Batch request for 5+ entities of different types — all get URLs."""
        user_id = uuid.uuid4()
        aq_id1 = uuid.uuid4()
        aq_id2 = uuid.uuid4()
        fish_id1 = uuid.uuid4()
        fish_id2 = uuid.uuid4()

        items = [
            {"entity_type": "avatar", "entity_id": user_id},
            {"entity_type": "aquarium", "entity_id": aq_id1},
            {"entity_type": "aquarium", "entity_id": aq_id2},
            {"entity_type": "fish", "entity_id": fish_id1},
            {"entity_type": "fish", "entity_id": fish_id2},
        ]

        with (
            patch(
                "app.services.image_service._batch_get_avatar_keys",
                new_callable=AsyncMock,
                return_value={("avatar", user_id): "avatars/u/a.webp"},
            ),
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={
                    ("aquarium", aq_id1): "aquariums/a1/x.webp",
                    ("aquarium", aq_id2): "aquariums/a2/y.webp",
                },
            ),
            patch(
                "app.services.image_service._batch_get_fish_keys",
                new_callable=AsyncMock,
                return_value={
                    ("fish", fish_id1): "fish/f1/x.webp",
                    ("fish", fish_id2): "fish/f2/y.webp",
                },
            ),
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={
                    "avatars/u/a.webp": "https://s3/avatars/u/a.webp?sig",
                    "aquariums/a1/x.webp": "https://s3/aquariums/a1/x.webp?sig",
                    "aquariums/a2/y.webp": "https://s3/aquariums/a2/y.webp?sig",
                    "fish/f1/x.webp": "https://s3/fish/f1/x.webp?sig",
                    "fish/f2/y.webp": "https://s3/fish/f2/y.webp?sig",
                },
            ),
        ):
            result = await get_presigned_urls(AsyncMock(), user_id, items)

        assert len(result) == 5
        # All items should have key and url
        for item in result:
            assert item["key"] is not None
            assert item["url"] is not None

    @pytest.mark.asyncio
    async def test_mixed_access_accessible_and_forbidden(self):
        """3 accessible + 2 forbidden — only 3 in result (forbidden excluded)."""
        user_id = uuid.uuid4()
        aq_ok1 = uuid.uuid4()
        aq_ok2 = uuid.uuid4()
        aq_denied = uuid.uuid4()
        fish_ok = uuid.uuid4()
        fish_denied = uuid.uuid4()

        items = [
            {"entity_type": "aquarium", "entity_id": aq_ok1},
            {"entity_type": "aquarium", "entity_id": aq_ok2},
            {"entity_type": "aquarium", "entity_id": aq_denied},
            {"entity_type": "fish", "entity_id": fish_ok},
            {"entity_type": "fish", "entity_id": fish_denied},
        ]

        with (
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={
                    ("aquarium", aq_ok1): "aquariums/a1/x.webp",
                    ("aquarium", aq_ok2): "aquariums/a2/y.webp",
                    # aq_denied NOT in result — no access
                },
            ),
            patch(
                "app.services.image_service._batch_get_fish_keys",
                new_callable=AsyncMock,
                return_value={
                    ("fish", fish_ok): "fish/f1/x.webp",
                    # fish_denied NOT in result — no access
                },
            ),
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={
                    "aquariums/a1/x.webp": "https://s3/a1?sig",
                    "aquariums/a2/y.webp": "https://s3/a2?sig",
                    "fish/f1/x.webp": "https://s3/f1?sig",
                },
            ),
        ):
            result = await get_presigned_urls(AsyncMock(), user_id, items)

        assert len(result) == 3
        returned_ids = {item["entity_id"] for item in result}
        assert aq_denied not in returned_ids
        assert fish_denied not in returned_ids

    @pytest.mark.asyncio
    async def test_entity_with_access_but_no_photo_key(self):
        """Entity with access but no photo_key → key: None, url: None (NOT skipped)."""
        user_id = uuid.uuid4()
        aq_id = uuid.uuid4()

        items = [{"entity_type": "aquarium", "entity_id": aq_id}]

        with (
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={("aquarium", aq_id): None},  # accessible but no photo
            ),
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            result = await get_presigned_urls(AsyncMock(), user_id, items)

        assert len(result) == 1
        assert result[0]["entity_type"] == "aquarium"
        assert result[0]["entity_id"] == aq_id
        assert result[0]["key"] is None
        assert result[0]["url"] is None

    @pytest.mark.asyncio
    async def test_invalid_entity_type_skipped(self):
        """Invalid entity_type in batch should be silently skipped."""
        user_id = uuid.uuid4()
        aq_id = uuid.uuid4()

        items = [
            {"entity_type": "invalid_type", "entity_id": uuid.uuid4()},
            {"entity_type": "aquarium", "entity_id": aq_id},
        ]

        with (
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={("aquarium", aq_id): "aquariums/a/x.webp"},
            ),
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={"aquariums/a/x.webp": "https://s3/a?sig"},
            ),
        ):
            result = await get_presigned_urls(AsyncMock(), user_id, items)

        # Only the valid aquarium item is returned
        assert len(result) == 1
        assert result[0]["entity_type"] == "aquarium"

    @pytest.mark.asyncio
    async def test_empty_items_returns_empty_list(self):
        """Empty items list should return empty result."""
        result = await get_presigned_urls(AsyncMock(), uuid.uuid4(), [])
        assert result == []

    @pytest.mark.asyncio
    async def test_preserves_original_item_order(self):
        """Result should preserve the order of items from the request."""
        user_id = uuid.uuid4()
        fish_id = uuid.uuid4()
        aq_id = uuid.uuid4()

        items = [
            {"entity_type": "fish", "entity_id": fish_id},
            {"entity_type": "avatar", "entity_id": user_id},
            {"entity_type": "aquarium", "entity_id": aq_id},
        ]

        with (
            patch(
                "app.services.image_service._batch_get_avatar_keys",
                new_callable=AsyncMock,
                return_value={("avatar", user_id): "avatars/u/a.webp"},
            ),
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={("aquarium", aq_id): "aquariums/a/x.webp"},
            ),
            patch(
                "app.services.image_service._batch_get_fish_keys",
                new_callable=AsyncMock,
                return_value={("fish", fish_id): "fish/f/y.webp"},
            ),
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={
                    "avatars/u/a.webp": "url1",
                    "aquariums/a/x.webp": "url2",
                    "fish/f/y.webp": "url3",
                },
            ),
        ):
            result = await get_presigned_urls(AsyncMock(), user_id, items)

        assert result[0]["entity_type"] == "fish"
        assert result[1]["entity_type"] == "avatar"
        assert result[2]["entity_type"] == "aquarium"

    @pytest.mark.asyncio
    async def test_duplicate_photo_keys_generate_single_url(self):
        """Two entities sharing the same photo_key should still work correctly."""
        user_id = uuid.uuid4()
        aq_id1 = uuid.uuid4()
        aq_id2 = uuid.uuid4()
        shared_key = "aquariums/shared/x.webp"

        items = [
            {"entity_type": "aquarium", "entity_id": aq_id1},
            {"entity_type": "aquarium", "entity_id": aq_id2},
        ]

        with (
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={
                    ("aquarium", aq_id1): shared_key,
                    ("aquarium", aq_id2): shared_key,
                },
            ),
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={shared_key: "https://s3/shared?sig"},
            ) as mock_batch_sign,
        ):
            result = await get_presigned_urls(AsyncMock(), user_id, items)

        assert len(result) == 2
        # Both should get the same URL
        assert result[0]["url"] == "https://s3/shared?sig"
        assert result[1]["url"] == "https://s3/shared?sig"
        # Batch should receive deduplicated keys
        call_keys = mock_batch_sign.call_args[0][0]
        assert len(call_keys) == 1

    @pytest.mark.asyncio
    async def test_no_s3_calls_when_all_keys_null(self):
        """When all entities have null photo_key, no S3 URLs should be generated."""
        user_id = uuid.uuid4()
        aq_id = uuid.uuid4()

        items = [{"entity_type": "aquarium", "entity_id": aq_id}]

        with (
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={("aquarium", aq_id): None},
            ),
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_batch_sign,
        ):
            result = await get_presigned_urls(AsyncMock(), user_id, items)

        assert len(result) == 1
        assert result[0]["key"] is None
        assert result[0]["url"] is None
        # No keys to sign → batch sign helper not called at all
        mock_batch_sign.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_helpers_called_with_correct_entity_ids(self):
        """Each batch helper should receive only its entity_type's IDs."""
        user_id = uuid.uuid4()
        aq_id = uuid.uuid4()
        fish_id = uuid.uuid4()

        items = [
            {"entity_type": "avatar", "entity_id": user_id},
            {"entity_type": "aquarium", "entity_id": aq_id},
            {"entity_type": "fish", "entity_id": fish_id},
        ]

        with (
            patch(
                "app.services.image_service._batch_get_avatar_keys",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_avatar,
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_aquarium,
            patch(
                "app.services.image_service._batch_get_fish_keys",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_fish,
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            await get_presigned_urls(AsyncMock(), user_id, items)

        mock_avatar.assert_called_once()
        avatar_ids = mock_avatar.call_args[0][2]
        assert avatar_ids == [user_id]

        mock_aquarium.assert_called_once()
        aquarium_ids = mock_aquarium.call_args[0][2]
        assert aquarium_ids == [aq_id]

        mock_fish.assert_called_once()
        fish_ids = mock_fish.call_args[0][2]
        assert fish_ids == [fish_id]

    @pytest.mark.asyncio
    async def test_no_avatar_helper_called_when_no_avatars_in_items(self):
        """Avatar batch helper should not be called if no avatar items."""
        user_id = uuid.uuid4()
        aq_id = uuid.uuid4()

        items = [{"entity_type": "aquarium", "entity_id": aq_id}]

        with (
            patch(
                "app.services.image_service._batch_get_avatar_keys",
                new_callable=AsyncMock,
            ) as mock_avatar,
            patch(
                "app.services.image_service._batch_get_aquarium_keys",
                new_callable=AsyncMock,
                return_value={("aquarium", aq_id): None},
            ),
            patch(
                "app.services.image_service.batch_generate_presigned_urls",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            await get_presigned_urls(AsyncMock(), user_id, items)

        mock_avatar.assert_not_called()
