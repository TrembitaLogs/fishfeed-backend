"""Tests for image preprocessing service."""

import base64
import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from app.services.image_processing import (
    ImageTooLargeError,
    InvalidBase64Error,
    InvalidImageFormatError,
    calculate_image_hash,
    decode_base64_image,
    preprocess_for_ai,
    process_upload_file,
)


def create_test_image(
    width: int = 100,
    height: int = 100,
    color: tuple = (255, 0, 0),
    mode: str = "RGB",
    format: str = "JPEG",
) -> bytes:
    """Create a test image and return as bytes."""
    image = Image.new(mode, (width, height), color)
    buffer = io.BytesIO()
    if format == "JPEG" and mode == "RGBA":
        # JPEG doesn't support RGBA, convert to RGB
        image = image.convert("RGB")
    image.save(buffer, format=format)
    buffer.seek(0)
    return buffer.getvalue()


def create_base64_image(
    width: int = 100,
    height: int = 100,
    format: str = "JPEG",
    with_prefix: bool = False,
) -> str:
    """Create a base64 encoded test image."""
    image_bytes = create_test_image(width=width, height=height, format=format)
    b64 = base64.b64encode(image_bytes).decode()
    if with_prefix:
        mime_type = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[format]
        return f"data:{mime_type};base64,{b64}"
    return b64


class TestDecodeBase64Image:
    """Tests for decode_base64_image function."""

    def test_decode_valid_jpeg(self):
        """Test decoding valid JPEG base64."""
        b64 = create_base64_image(format="JPEG")
        image = decode_base64_image(b64)
        assert isinstance(image, Image.Image)
        assert image.size == (100, 100)

    def test_decode_valid_png(self):
        """Test decoding valid PNG base64."""
        b64 = create_base64_image(format="PNG")
        image = decode_base64_image(b64)
        assert isinstance(image, Image.Image)
        assert image.size == (100, 100)

    def test_decode_valid_webp(self):
        """Test decoding valid WebP base64."""
        b64 = create_base64_image(format="WEBP")
        image = decode_base64_image(b64)
        assert isinstance(image, Image.Image)
        assert image.size == (100, 100)

    def test_decode_with_data_uri_prefix(self):
        """Test decoding base64 with data URI prefix."""
        b64 = create_base64_image(format="JPEG", with_prefix=True)
        image = decode_base64_image(b64)
        assert isinstance(image, Image.Image)

    def test_decode_empty_string_raises_error(self):
        """Test that empty string raises InvalidBase64Error."""
        with pytest.raises(InvalidBase64Error) as exc_info:
            decode_base64_image("")
        assert "Empty base64 string" in str(exc_info.value)

    def test_decode_invalid_base64_raises_error(self):
        """Test that invalid base64 raises InvalidBase64Error."""
        with pytest.raises(InvalidBase64Error) as exc_info:
            decode_base64_image("not-valid-base64!!!")
        assert exc_info.value.status_code == 400

    def test_decode_non_image_data_raises_error(self):
        """Test that non-image data raises InvalidImageFormatError."""
        text_b64 = base64.b64encode(b"This is just text, not an image").decode()
        with pytest.raises(InvalidImageFormatError):
            decode_base64_image(text_b64)


class TestProcessUploadFile:
    """Tests for process_upload_file function."""

    @pytest.mark.asyncio
    async def test_process_valid_jpeg_upload(self):
        """Test processing valid JPEG upload."""
        image_bytes = create_test_image(format="JPEG")
        mock_file = AsyncMock()
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=image_bytes)

        image = await process_upload_file(mock_file)
        assert isinstance(image, Image.Image)

    @pytest.mark.asyncio
    async def test_process_valid_png_upload(self):
        """Test processing valid PNG upload."""
        image_bytes = create_test_image(format="PNG")
        mock_file = AsyncMock()
        mock_file.content_type = "image/png"
        mock_file.read = AsyncMock(return_value=image_bytes)

        image = await process_upload_file(mock_file)
        assert isinstance(image, Image.Image)

    @pytest.mark.asyncio
    async def test_process_valid_webp_upload(self):
        """Test processing valid WebP upload."""
        image_bytes = create_test_image(format="WEBP")
        mock_file = AsyncMock()
        mock_file.content_type = "image/webp"
        mock_file.read = AsyncMock(return_value=image_bytes)

        image = await process_upload_file(mock_file)
        assert isinstance(image, Image.Image)

    @pytest.mark.asyncio
    async def test_invalid_mime_type_raises_error(self):
        """Test that invalid MIME type raises InvalidImageFormatError."""
        mock_file = AsyncMock()
        mock_file.content_type = "text/plain"
        mock_file.read = AsyncMock(return_value=b"not an image")

        with pytest.raises(InvalidImageFormatError) as exc_info:
            await process_upload_file(mock_file)
        assert "text/plain" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_gif_not_allowed(self):
        """Test that GIF MIME type is not allowed."""
        mock_file = AsyncMock()
        mock_file.content_type = "image/gif"
        mock_file.read = AsyncMock(return_value=b"GIF89a...")

        with pytest.raises(InvalidImageFormatError):
            await process_upload_file(mock_file)

    @pytest.mark.asyncio
    async def test_file_too_large_raises_error(self):
        """Test that file exceeding size limit raises ImageTooLargeError."""
        # Create a mock that returns data larger than 10MB
        large_data = b"x" * (11 * 1024 * 1024)  # 11MB
        mock_file = AsyncMock()
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=large_data)

        with pytest.raises(ImageTooLargeError) as exc_info:
            await process_upload_file(mock_file)
        assert exc_info.value.status_code == 413
        assert "10MB" in str(exc_info.value)


class TestPreprocessForAi:
    """Tests for preprocess_for_ai function."""

    def test_resize_preserves_aspect_ratio_landscape(self):
        """Test that landscape images preserve aspect ratio."""
        # Create 200x100 landscape image
        image_bytes = create_test_image(width=200, height=100)
        image = Image.open(io.BytesIO(image_bytes))

        result = preprocess_for_ai(image)
        result_image = Image.open(io.BytesIO(result))

        # Output should be 512x512
        assert result_image.size == (512, 512)

    def test_resize_preserves_aspect_ratio_portrait(self):
        """Test that portrait images preserve aspect ratio."""
        # Create 100x200 portrait image
        image_bytes = create_test_image(width=100, height=200)
        image = Image.open(io.BytesIO(image_bytes))

        result = preprocess_for_ai(image)
        result_image = Image.open(io.BytesIO(result))

        # Output should be 512x512
        assert result_image.size == (512, 512)

    def test_resize_square_image(self):
        """Test resizing square image."""
        image_bytes = create_test_image(width=100, height=100)
        image = Image.open(io.BytesIO(image_bytes))

        result = preprocess_for_ai(image)
        result_image = Image.open(io.BytesIO(result))

        assert result_image.size == (512, 512)

    def test_larger_image_downscaled(self):
        """Test that larger images are downscaled."""
        image_bytes = create_test_image(width=1000, height=1000)
        image = Image.open(io.BytesIO(image_bytes))

        result = preprocess_for_ai(image)
        result_image = Image.open(io.BytesIO(result))

        assert result_image.size == (512, 512)

    def test_rgba_converted_to_rgb(self):
        """Test that RGBA images are converted to RGB."""
        image_bytes = create_test_image(mode="RGBA", format="PNG")
        image = Image.open(io.BytesIO(image_bytes))
        assert image.mode == "RGBA"

        result = preprocess_for_ai(image)
        result_image = Image.open(io.BytesIO(result))

        assert result_image.mode == "RGB"

    def test_grayscale_converted_to_rgb(self):
        """Test that grayscale images are converted to RGB."""
        # Create grayscale image
        gray_image = Image.new("L", (100, 100), 128)
        buffer = io.BytesIO()
        gray_image.save(buffer, format="PNG")
        buffer.seek(0)
        image = Image.open(buffer)
        assert image.mode == "L"

        result = preprocess_for_ai(image)
        result_image = Image.open(io.BytesIO(result))

        assert result_image.mode == "RGB"

    def test_output_is_jpeg_bytes(self):
        """Test that output is JPEG format bytes."""
        image_bytes = create_test_image(format="PNG")
        image = Image.open(io.BytesIO(image_bytes))

        result = preprocess_for_ai(image)

        # JPEG files start with FFD8
        assert result[:2] == b"\xff\xd8"

    def test_padding_is_white(self):
        """Test that padding uses white background."""
        # Create narrow image
        narrow_image = Image.new("RGB", (100, 50), (255, 0, 0))  # Red

        result = preprocess_for_ai(narrow_image)
        result_image = Image.open(io.BytesIO(result))

        # Check corner pixel (should be white padding)
        corner_pixel = result_image.getpixel((0, 0))
        # JPEG compression may slightly alter colors
        assert corner_pixel[0] > 250  # R close to 255
        assert corner_pixel[1] > 250  # G close to 255
        assert corner_pixel[2] > 250  # B close to 255


class TestCalculateImageHash:
    """Tests for calculate_image_hash function."""

    def test_hash_returns_hex_string(self):
        """Test that hash returns hexadecimal string."""
        image_bytes = create_test_image()
        hash_result = calculate_image_hash(image_bytes)

        assert isinstance(hash_result, str)
        assert len(hash_result) == 64  # SHA-256 produces 64 hex chars

    def test_same_image_same_hash(self):
        """Test that identical images produce identical hashes."""
        image_bytes = create_test_image(width=100, height=100, color=(255, 0, 0))

        hash1 = calculate_image_hash(image_bytes)
        hash2 = calculate_image_hash(image_bytes)

        assert hash1 == hash2

    def test_different_images_different_hash(self):
        """Test that different images produce different hashes."""
        image1 = create_test_image(color=(255, 0, 0))  # Red
        image2 = create_test_image(color=(0, 255, 0))  # Green

        hash1 = calculate_image_hash(image1)
        hash2 = calculate_image_hash(image2)

        assert hash1 != hash2

    def test_hash_consistency(self):
        """Test hash consistency across multiple calls."""
        image_bytes = b"test image data for hashing"
        expected_hash = calculate_image_hash(image_bytes)

        for _ in range(10):
            assert calculate_image_hash(image_bytes) == expected_hash


class TestIntegration:
    """Integration tests combining multiple functions."""

    def test_base64_decode_and_preprocess(self):
        """Test full pipeline from base64 to preprocessed bytes."""
        # Create and encode image
        b64 = create_base64_image(width=200, height=100, format="JPEG")

        # Decode
        image = decode_base64_image(b64)
        assert image.size == (200, 100)

        # Preprocess
        result = preprocess_for_ai(image)
        result_image = Image.open(io.BytesIO(result))
        assert result_image.size == (512, 512)
        assert result_image.mode == "RGB"

    def test_hash_after_preprocess_is_consistent(self):
        """Test that hash of preprocessed image is consistent."""
        b64 = create_base64_image(width=150, height=150, format="PNG")

        image = decode_base64_image(b64)
        result1 = preprocess_for_ai(image)
        result2 = preprocess_for_ai(image)

        # Same preprocessing should yield same hash
        hash1 = calculate_image_hash(result1)
        hash2 = calculate_image_hash(result2)
        assert hash1 == hash2

    @pytest.mark.asyncio
    async def test_upload_and_preprocess(self):
        """Test full pipeline from upload to preprocessed bytes."""
        image_bytes = create_test_image(width=300, height=200, format="JPEG")

        mock_file = AsyncMock()
        mock_file.content_type = "image/jpeg"
        mock_file.read = AsyncMock(return_value=image_bytes)

        # Process upload
        image = await process_upload_file(mock_file)
        assert image.size == (300, 200)

        # Preprocess
        result = preprocess_for_ai(image)
        result_image = Image.open(io.BytesIO(result))
        assert result_image.size == (512, 512)
