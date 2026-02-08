"""Image preprocessing service for AI fish recognition."""

import base64
import hashlib
import io

import structlog
from fastapi import UploadFile
from PIL import Image

from app.config import get_settings

logger = structlog.get_logger(__name__)

ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class ImageProcessingError(Exception):
    """Base exception for image processing errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class InvalidBase64Error(ImageProcessingError):
    """Raised when base64 string is invalid."""

    def __init__(self, detail: str = "Invalid base64 encoded image"):
        super().__init__(detail, status_code=400)


class InvalidImageFormatError(ImageProcessingError):
    """Raised when image format is not supported."""

    def __init__(self, detail: str = "Unsupported image format"):
        super().__init__(detail, status_code=400)


class ImageTooLargeError(ImageProcessingError):
    """Raised when image exceeds size limit."""

    def __init__(self, max_size_mb: int):
        super().__init__(
            f"Image size exceeds maximum allowed size of {max_size_mb}MB",
            status_code=413,
        )


def decode_base64_image(base64_str: str) -> Image.Image:
    """Decode base64 string to PIL Image.

    Args:
        base64_str: Base64 encoded image data. May include data URI prefix.

    Returns:
        PIL Image object.

    Raises:
        InvalidBase64Error: If base64 string cannot be decoded.
        InvalidImageFormatError: If decoded data is not a valid image.
    """
    if not base64_str:
        raise InvalidBase64Error("Empty base64 string")

    # Remove data URI prefix if present (e.g., "data:image/jpeg;base64,")
    if "," in base64_str and base64_str.startswith("data:"):
        base64_str = base64_str.split(",", 1)[1]

    try:
        image_data = base64.b64decode(base64_str)
    except Exception as e:
        logger.warning(f"Failed to decode base64: {e}")
        raise InvalidBase64Error("Failed to decode base64 string") from e

    return _bytes_to_image(image_data)


async def process_upload_file(file: UploadFile) -> Image.Image:
    """Process uploaded file to PIL Image.

    Args:
        file: FastAPI UploadFile object.

    Returns:
        PIL Image object.

    Raises:
        InvalidImageFormatError: If file MIME type is not supported.
        ImageTooLargeError: If file exceeds size limit.
    """
    settings = get_settings()
    max_size_bytes = settings.MAX_IMAGE_SIZE_MB * 1024 * 1024

    # Validate MIME type
    content_type = file.content_type or ""
    if content_type not in ALLOWED_MIME_TYPES:
        raise InvalidImageFormatError(
            f"Unsupported content type: {content_type}. "
            f"Allowed: {', '.join(ALLOWED_MIME_TYPES)}"
        )

    # Read file content with size check
    content = await file.read()

    if len(content) > max_size_bytes:
        raise ImageTooLargeError(settings.MAX_IMAGE_SIZE_MB)

    return _bytes_to_image(content)


def preprocess_for_ai(image: Image.Image) -> bytes:
    """Preprocess image for AI service.

    Resizes image to target size while preserving aspect ratio,
    adds padding if necessary, and converts to RGB.

    Args:
        image: PIL Image to preprocess.

    Returns:
        Preprocessed image as JPEG bytes.
    """
    settings = get_settings()
    target_size = settings.AI_IMAGE_SIZE

    # Convert to RGB if necessary (handles RGBA, P, L modes)
    if image.mode != "RGB":
        # Create white background for transparent images
        if image.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode == "P":
                image = image.convert("RGBA")
            background.paste(image, mask=image.split()[-1] if image.mode == "RGBA" else None)
            image = background
        else:
            image = image.convert("RGB")

    # Calculate new size preserving aspect ratio
    original_width, original_height = image.size
    ratio = min(target_size / original_width, target_size / original_height)
    new_width = int(original_width * ratio)
    new_height = int(original_height * ratio)

    # Resize with high-quality resampling
    resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

    # Create padded image with white background
    padded = Image.new("RGB", (target_size, target_size), (255, 255, 255))
    paste_x = (target_size - new_width) // 2
    paste_y = (target_size - new_height) // 2
    padded.paste(resized, (paste_x, paste_y))

    # Convert to JPEG bytes
    buffer = io.BytesIO()
    padded.save(buffer, format="JPEG", quality=90)
    buffer.seek(0)

    return buffer.getvalue()


def calculate_image_hash(image_bytes: bytes) -> str:
    """Calculate SHA-256 hash of image bytes for deduplication.

    Args:
        image_bytes: Raw image bytes.

    Returns:
        Hexadecimal SHA-256 hash string.
    """
    return hashlib.sha256(image_bytes).hexdigest()


def _bytes_to_image(image_data: bytes) -> Image.Image:
    """Convert bytes to PIL Image with validation.

    Args:
        image_data: Raw image bytes.

    Returns:
        PIL Image object.

    Raises:
        InvalidImageFormatError: If data is not a valid image.
    """
    try:
        image = Image.open(io.BytesIO(image_data))
        image.verify()  # Verify image integrity
        # Re-open because verify() can leave file in unusable state
        image = Image.open(io.BytesIO(image_data))
        image.load()  # Force load to catch truncated images
    except Exception as e:
        logger.warning(f"Failed to parse image: {e}")
        raise InvalidImageFormatError("Invalid or corrupted image data") from e

    # Check format
    if image.format and image.format.lower() not in {"jpeg", "png", "webp"}:
        raise InvalidImageFormatError(
            f"Unsupported image format: {image.format}. "
            "Allowed: JPEG, PNG, WebP"
        )

    return image
