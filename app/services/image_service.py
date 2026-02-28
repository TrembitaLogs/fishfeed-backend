"""Image service for entity photo management.

Provides async functions for uploading, validating, and generating
presigned URLs for entity images (aquariums, fish, avatars)
stored in S3-compatible storage (fishfeed-images bucket).

This module uses standalone async functions (not a class) following
the backend architectural pattern.
"""

import io
import uuid

import aioboto3
import structlog
from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.models.orphaned_image import OrphanedImage
from app.models.user import User
from app.services.aquarium import (
    AquariumAccessDeniedError,
    AquariumNotFoundError,
    check_access,
)

logger = structlog.get_logger(__name__)


# --- Exceptions ---


class ImageServiceError(Exception):
    """Base exception for image service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class UnsupportedMediaTypeError(ImageServiceError):
    """Raised when file format is not supported (not JPEG, PNG, or WebP)."""

    def __init__(self, detail: str = "Unsupported image format. Allowed: JPEG, PNG, WebP"):
        super().__init__(detail, status_code=415)


class FileTooLargeError(ImageServiceError):
    """Raised when file size exceeds the limit for entity type."""

    def __init__(self, entity_type: str, max_size_mb: int):
        super().__init__(
            f"File size exceeds maximum of {max_size_mb}MB for {entity_type}",
            status_code=413,
        )


class EntityNotFoundError(ImageServiceError):
    """Raised when the target entity is not found in the database."""

    def __init__(self, entity_type: str, entity_id: uuid.UUID):
        super().__init__(
            f"{entity_type.capitalize()} with id '{entity_id}' not found",
            status_code=404,
        )


class AccessDeniedError(ImageServiceError):
    """Raised when user doesn't have access to the entity."""

    def __init__(self, entity_type: str, entity_id: uuid.UUID):
        super().__init__(
            f"Access denied to {entity_type} '{entity_id}'",
            status_code=403,
        )


# --- Constants ---

# Magic bytes signatures for supported image formats.
# WebP is handled separately in detect_content_type() due to its
# RIFF container structure (shared with WAV, AVI, etc.).
MAGIC_BYTES: dict[bytes, str] = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
}

# S3 key prefix mapping for entity types
PREFIX_MAP: dict[str, str] = {
    "avatar": "avatars",
    "aquarium": "aquariums",
    "fish": "fish",
}

# Valid entity types for image operations
VALID_ENTITY_TYPES = frozenset(PREFIX_MAP.keys())

# Pillow format strings keyed by MIME type (for re-saving without EXIF)
MIME_TO_PILLOW_FORMAT: dict[str, str] = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}

# Decompression bomb protection: limit maximum pixel count.
# Default Pillow threshold is ~178M pixels; we halve it for extra safety.
# Images exceeding this trigger DecompressionBombWarning (1x) or
# DecompressionBombError (2x). Our explicit dimension check catches
# oversized images well before the Pillow threshold fires.
Image.MAX_IMAGE_PIXELS = 89_478_485


# --- Helper functions ---


def get_image_limits() -> dict[str, dict[str, int]]:
    """Build per-entity-type image limits from application settings.

    Reads from config rather than hardcoding, so limits can be
    overridden via environment variables.

    Returns:
        Dict mapping entity_type to {"max_size": bytes, "max_dim": pixels}.
    """
    settings = get_settings()
    return {
        "avatar": {
            "max_size": settings.AVATAR_MAX_SIZE_MB * 1024 * 1024,
            "max_dim": settings.AVATAR_MAX_DIMENSION,
        },
        "aquarium": {
            "max_size": settings.AQUARIUM_PHOTO_MAX_SIZE_MB * 1024 * 1024,
            "max_dim": settings.AQUARIUM_PHOTO_MAX_DIMENSION,
        },
        "fish": {
            "max_size": settings.FISH_PHOTO_MAX_SIZE_MB * 1024 * 1024,
            "max_dim": settings.FISH_PHOTO_MAX_DIMENSION,
        },
    }


def _get_s3_session() -> aioboto3.Session:
    """Create a new aioboto3 session for S3 operations.

    Returns:
        aioboto3.Session instance.
    """
    return aioboto3.Session()


def _get_s3_client_config() -> dict:
    """Get S3 client configuration from application settings.

    Uses shared credentials (same as S3StorageService for AI scans)
    but targets the images bucket (S3_IMAGES_BUCKET_NAME).

    Returns:
        Dict with boto3 client configuration parameters.
    """
    settings = get_settings()
    return {
        "service_name": "s3",
        "endpoint_url": settings.S3_ENDPOINT_URL,
        "aws_access_key_id": settings.S3_ACCESS_KEY,
        "aws_secret_access_key": settings.S3_SECRET_KEY,
        "region_name": settings.S3_REGION,
    }


def detect_content_type(content: bytes) -> str:
    """Detect image content type from file magic bytes.

    Checks actual file bytes (not Content-Type header) to determine
    the real format. This prevents polyglot file attacks where a
    malicious file has a misleading Content-Type header.

    Args:
        content: Raw file bytes (at least 12 bytes required for WebP detection).

    Returns:
        MIME type string ("image/jpeg", "image/png", or "image/webp").

    Raises:
        UnsupportedMediaTypeError: If format is not JPEG, PNG, or WebP.
    """
    if len(content) < 12:
        raise UnsupportedMediaTypeError("File too small to be a valid image")

    # Check JPEG and PNG magic bytes
    for magic, mime_type in MAGIC_BYTES.items():
        if content.startswith(magic):
            return mime_type

    # Check WebP: RIFF container with WEBP identifier at byte offset 8.
    # RIFF alone is not sufficient — WAV and AVI also use RIFF containers.
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"

    raise UnsupportedMediaTypeError()


def generate_short_uuid() -> str:
    """Generate a short UUID for S3 object keys.

    Returns:
        First 8 hex characters of a UUID4 (e.g., "f7a3b2c1").
    """
    return uuid.uuid4().hex[:8]


def build_object_key(entity_type: str, entity_id: uuid.UUID, extension: str = ".webp") -> str:
    """Build S3 object key for an entity image.

    Args:
        entity_type: One of "avatar", "aquarium", "fish".
        entity_id: UUID of the entity.
        extension: File extension including dot (default ".webp").

    Returns:
        S3 object key (e.g., "aquariums/550e8400-.../f7a3b2c1.webp").

    Raises:
        ValueError: If entity_type is not valid.
    """
    if entity_type not in VALID_ENTITY_TYPES:
        msg = f"Invalid entity_type: {entity_type}. Must be one of {sorted(VALID_ENTITY_TYPES)}"
        raise ValueError(msg)

    prefix = PREFIX_MAP[entity_type]
    short_id = generate_short_uuid()
    return f"{prefix}/{entity_id}/{short_id}{extension}"


# --- Image validation ---


def _has_exif(img: Image.Image) -> bool:
    """Check if image contains EXIF metadata that should be stripped.

    EXIF may contain private data (GPS coordinates, camera serial number,
    etc.). WebP conversion on the client strips EXIF by default, but
    JPEG/PNG uploads may still carry it.

    Args:
        img: Opened PIL Image (after load()).

    Returns:
        True if EXIF or privacy-relevant metadata is present.
    """
    # Raw EXIF bytes (present in JPEG, WebP)
    if img.info.get("exif"):
        return True
    # Parsed EXIF dict (more thorough — catches edge cases)
    if img.getexif():
        return True
    return False


def _strip_exif(img: Image.Image, content_type: str) -> bytes:
    """Re-save image without EXIF metadata, preserving format and ICC profile.

    The image is re-encoded in its original format. For lossy formats
    (JPEG, WebP) quality=95 minimises re-compression artefacts.
    ICC profiles are preserved because they affect colour rendering
    and do not contain private information.

    Args:
        img: Opened PIL Image (after load()).
        content_type: MIME type ("image/jpeg", "image/png", "image/webp").

    Returns:
        Re-encoded image bytes without EXIF.
    """
    pillow_format = MIME_TO_PILLOW_FORMAT[content_type]
    buf = io.BytesIO()

    save_kwargs: dict[str, object] = {}

    # Preserve ICC profile (colour space, not privacy-sensitive)
    icc_profile = img.info.get("icc_profile")
    if icc_profile:
        save_kwargs["icc_profile"] = icc_profile

    # High quality for lossy formats to minimise re-compression loss
    if pillow_format in ("JPEG", "WEBP"):
        save_kwargs["quality"] = 95

    img.save(buf, format=pillow_format, **save_kwargs)
    return buf.getvalue()


def validate_image(content: bytes, entity_type: str) -> tuple[str, bytes]:
    """Validate image content and strip EXIF metadata if present.

    Performs full server-side validation:
    1. Magic bytes check (real format, not Content-Type header)
    2. File size check against per-entity-type limits
    3. Pillow decode with decompression bomb protection
    4. Dimension check against per-entity-type limits
    5. EXIF stripping if metadata is present

    The server does NOT convert the image format — format is preserved
    as-is (JPEG→JPEG, PNG→PNG, WebP→WebP). WebP conversion is the
    client's responsibility per the architectural document.

    Args:
        content: Raw image file bytes.
        entity_type: One of "avatar", "aquarium", "fish".

    Returns:
        Tuple of (content_type, processed_bytes) where content_type is
        the detected MIME type and processed_bytes has EXIF stripped
        if it was present (otherwise original bytes are returned).

    Raises:
        UnsupportedMediaTypeError: If format is not JPEG, PNG, or WebP.
        FileTooLargeError: If file size exceeds limit for entity_type.
        ValueError: If entity_type is invalid, dimensions exceed limit,
            image is corrupted, or decompression bomb is detected.
    """
    # 1. Validate entity_type
    if entity_type not in VALID_ENTITY_TYPES:
        msg = f"Invalid entity_type: {entity_type}. Must be one of {sorted(VALID_ENTITY_TYPES)}"
        raise ValueError(msg)

    # 2. Detect real content type from magic bytes
    content_type = detect_content_type(content)

    # 3. Check file size against per-type limits
    limits = get_image_limits()
    type_limits = limits[entity_type]
    if len(content) > type_limits["max_size"]:
        max_mb = type_limits["max_size"] // (1024 * 1024)
        raise FileTooLargeError(entity_type, max_mb)

    # 4. Open image (reads header only — no full decompression yet)
    try:
        img = Image.open(io.BytesIO(content))
    except Image.DecompressionBombError as exc:
        raise ValueError(
            "Image exceeds maximum pixel count (potential decompression bomb)"
        ) from exc
    except Exception as exc:
        raise ValueError(f"Invalid or corrupted image: {exc}") from exc

    # 5. Check dimensions from header BEFORE load() to prevent
    #    allocating memory for oversized images
    width, height = img.size
    max_dim = type_limits["max_dim"]
    if width > max_dim or height > max_dim:
        raise ValueError(
            f"Image dimensions {width}x{height} exceed maximum "
            f"{max_dim}x{max_dim} for {entity_type}"
        )

    # 6. Fully decode the image (catches truncated/corrupt data)
    try:
        img.load()
    except Image.DecompressionBombError as exc:
        raise ValueError(
            "Image exceeds maximum pixel count (potential decompression bomb)"
        ) from exc
    except Exception as exc:
        raise ValueError(f"Invalid or corrupted image: {exc}") from exc

    # 7. Strip EXIF metadata if present (privacy: GPS, camera serial, etc.)
    if _has_exif(img):
        content = _strip_exif(img, content_type)
        logger.info(
            "stripped_exif_metadata",
            entity_type=entity_type,
            content_type=content_type,
        )

    return content_type, content


# --- Orphaned image tracking ---


async def register_orphaned(db: AsyncSession, old_key: str, entity_type: str) -> None:
    """Register an orphaned S3 key for deferred cleanup.

    Called when a photo is replaced or removed. The old key is saved
    in the orphaned_images table. A background job will delete it
    from S3 after a grace period (7 days).

    Args:
        db: Database session (should be within a transaction).
        old_key: S3 object key that is no longer referenced.
        entity_type: One of "avatar", "aquarium", "fish".
    """
    orphan = OrphanedImage(old_key=old_key, entity_type=entity_type)
    db.add(orphan)
    logger.info(
        "registered_orphaned_image",
        old_key=old_key,
        entity_type=entity_type,
    )


# --- Access checking ---


async def _check_entity_access(
    db: AsyncSession,
    user_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
) -> None:
    """Verify user has access to upload an image for the given entity.

    Distinguishes between "entity not found" (404) and "access denied" (403).

    Args:
        db: Database session.
        user_id: Authenticated user's ID (from JWT).
        entity_type: One of "avatar", "aquarium", "fish".
        entity_id: UUID of the target entity.

    Raises:
        EntityNotFoundError: If the entity does not exist.
        AccessDeniedError: If the user lacks permission.
    """
    if entity_type == "avatar":
        # Avatar: only the user themselves can upload
        if user_id != entity_id:
            raise AccessDeniedError(entity_type, entity_id)
        # Verify user exists (defensive — authenticated users always exist)
        stmt = select(User).where(User.id == entity_id, User.deleted_at.is_(None))
        result = await db.execute(stmt)
        if result.scalar_one_or_none() is None:
            raise EntityNotFoundError(entity_type, entity_id)

    elif entity_type == "aquarium":
        # Aquarium: owner or member via check_access
        try:
            await check_access(db, entity_id, user_id)
        except AquariumNotFoundError:
            raise EntityNotFoundError(entity_type, entity_id) from None
        except AquariumAccessDeniedError:
            raise AccessDeniedError(entity_type, entity_id) from None

    elif entity_type == "fish":
        # Fish: first verify fish exists, then check aquarium access
        fish_stmt = select(Fish).where(Fish.id == entity_id, Fish.deleted_at.is_(None))
        fish_result = await db.execute(fish_stmt)
        fish = fish_result.scalar_one_or_none()
        if fish is None:
            raise EntityNotFoundError(entity_type, entity_id)
        try:
            await check_access(db, fish.aquarium_id, user_id)
        except AquariumNotFoundError:
            # Fish's aquarium was deleted — treat as fish not found
            raise EntityNotFoundError(entity_type, entity_id) from None
        except AquariumAccessDeniedError:
            raise AccessDeniedError(entity_type, entity_id) from None


# --- S3 upload ---


async def _upload_to_s3(object_key: str, content: bytes, content_type: str) -> None:
    """Upload image bytes to S3 (fishfeed-images bucket).

    Content-Type is set from magic bytes detection (not client header)
    to ensure correct MIME type for presigned URL downloads.

    Args:
        object_key: S3 object key (e.g., "aquariums/{id}/{uuid}.webp").
        content: Validated image bytes.
        content_type: MIME type detected from magic bytes.
    """
    settings = get_settings()
    session = _get_s3_session()
    config = _get_s3_client_config()

    async with session.client(**config) as s3:
        await s3.put_object(
            Bucket=settings.S3_IMAGES_BUCKET_NAME,
            Key=object_key,
            Body=content,
            ContentType=content_type,
        )

    logger.info(
        "uploaded_image_to_s3",
        key=object_key,
        content_type=content_type,
        size=len(content),
    )


# --- Atomic DB update ---


async def _update_entity_photo_key(
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    new_key: str,
) -> None:
    """Atomically update entity's photo_key with FOR UPDATE row lock.

    Sequence:
    1. SELECT entity FOR UPDATE (acquires row lock)
    2. If old photo_key/avatar_key exists, register as orphaned
    3. Set new key
    4. Flush (caller manages commit)

    The FOR UPDATE lock is acquired AFTER S3 upload completes to
    minimise lock hold time (per architectural document).

    Args:
        db: Database session.
        entity_type: One of "avatar", "aquarium", "fish".
        entity_id: UUID of the entity.
        new_key: New S3 object key.
    """
    old_key: str | None = None
    if entity_type == "avatar":
        user_stmt = select(User).where(User.id == entity_id).with_for_update()
        user_result = await db.execute(user_stmt)
        user_entity = user_result.scalar_one()
        old_key = user_entity.avatar_key
        user_entity.avatar_key = new_key
    elif entity_type == "aquarium":
        aq_stmt = select(Aquarium).where(Aquarium.id == entity_id).with_for_update()
        aq_result = await db.execute(aq_stmt)
        aq_entity = aq_result.scalar_one()
        old_key = aq_entity.photo_key
        aq_entity.photo_key = new_key
    elif entity_type == "fish":
        fish_stmt = select(Fish).where(Fish.id == entity_id).with_for_update()
        fish_result = await db.execute(fish_stmt)
        fish_entity = fish_result.scalar_one()
        old_key = fish_entity.photo_key
        fish_entity.photo_key = new_key
    else:
        msg = f"Invalid entity_type: {entity_type}"
        raise ValueError(msg)

    if old_key:
        await register_orphaned(db, old_key, entity_type)

    await db.flush()

    logger.info(
        "updated_entity_photo_key",
        entity_type=entity_type,
        entity_id=str(entity_id),
        old_key=old_key,
        new_key=new_key,
    )


# --- Main upload function ---


async def upload_image(
    db: AsyncSession,
    user_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    file_content: bytes,
) -> str:
    """Upload an image for an entity with access check and atomic DB update.

    Performs the full upload pipeline:
    1. Validate entity_type
    2. Check user access to the entity
    3. Validate image (magic bytes, size, dimensions, EXIF strip)
    4. Generate unique S3 key
    5. Upload to S3 (BEFORE DB lock to minimise lock hold time)
    6. Atomic DB update: lock entity -> orphan old key -> set new key

    If S3 upload succeeds but DB update fails, the uploaded file
    remains as orphaned in S3 — the weekly reconciliation job will
    clean it up (acceptable edge case per architectural document).

    Args:
        db: Database session.
        user_id: Authenticated user's UUID (from JWT).
        entity_type: One of "avatar", "aquarium", "fish".
        entity_id: UUID of the target entity.
        file_content: Raw image bytes.

    Returns:
        S3 object key (e.g., "aquariums/{id}/{uuid}.webp").

    Raises:
        ValueError: If entity_type is invalid.
        EntityNotFoundError: If entity is not found (404).
        AccessDeniedError: If user lacks permission (403).
        UnsupportedMediaTypeError: If format is not JPEG/PNG/WebP (415).
        FileTooLargeError: If file exceeds size limit (413).
    """
    # 1. Validate entity_type
    if entity_type not in VALID_ENTITY_TYPES:
        msg = f"Invalid entity_type: {entity_type}. Must be one of {sorted(VALID_ENTITY_TYPES)}"
        raise ValueError(msg)

    # 2. Check access (raises EntityNotFoundError or AccessDeniedError)
    await _check_entity_access(db, user_id, entity_type, entity_id)

    # 3. Validate image (magic bytes, size, dimensions, strip EXIF)
    content_type, processed_bytes = validate_image(file_content, entity_type)

    # 4. Generate S3 key (always .webp extension per architecture doc)
    object_key = build_object_key(entity_type, entity_id)

    # 5. Upload to S3 (BEFORE DB transaction to minimise lock time)
    await _upload_to_s3(object_key, processed_bytes, content_type)

    # 6. Atomic DB update: FOR UPDATE -> orphan old key -> update photo_key
    await _update_entity_photo_key(db, entity_type, entity_id, object_key)

    logger.info(
        "image_upload_completed",
        entity_type=entity_type,
        entity_id=str(entity_id),
        key=object_key,
        user_id=str(user_id),
    )

    return object_key


# --- Presigned URL generation ---


async def generate_presigned_url(key: str, expires_in: int = 3600) -> str:
    """Generate a presigned GET URL for an S3 object in the images bucket.

    Uses S3_PRESIGNED_ENDPOINT_URL (if configured) instead of S3_ENDPOINT_URL
    for generating presigned URLs. This ensures URLs are accessible from
    outside the Docker network — in dev, MinIO is at http://minio:9000
    internally but mapped to http://localhost:9000 for external access.

    Args:
        key: S3 object key (e.g., "aquariums/{id}/{uuid}.webp").
        expires_in: URL expiration time in seconds (default 1 hour).

    Returns:
        Presigned URL string.

    Raises:
        ImageServiceError: If URL generation fails.
    """
    settings = get_settings()
    session = _get_s3_session()
    endpoint_url = settings.S3_PRESIGNED_ENDPOINT_URL or settings.S3_ENDPOINT_URL

    config = {
        "service_name": "s3",
        "endpoint_url": endpoint_url,
        "aws_access_key_id": settings.S3_ACCESS_KEY,
        "aws_secret_access_key": settings.S3_SECRET_KEY,
        "region_name": settings.S3_REGION,
    }

    try:
        async with session.client(**config) as s3:
            url = await s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.S3_IMAGES_BUCKET_NAME,
                    "Key": key,
                    "ResponseContentDisposition": "inline",
                },
                ExpiresIn=expires_in,
            )
        return str(url)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "presigned_url_generation_failed",
            key=key,
            error_code=error_code,
        )
        raise ImageServiceError(
            f"Failed to generate presigned URL: {error_code}",
            status_code=500,
        ) from exc


# --- Batch presigned URL helpers ---


async def batch_generate_presigned_urls(
    keys: list[str],
    expires_in: int = 3600,
) -> dict[str, str]:
    """Generate presigned GET URLs for multiple S3 keys in one client session.

    Reuses a single S3 client for all URL generations to reduce overhead.

    Args:
        keys: List of S3 object keys.
        expires_in: URL expiration time in seconds (default 1 hour).

    Returns:
        Dict mapping S3 key to presigned URL.
    """
    if not keys:
        return {}

    settings = get_settings()
    session = _get_s3_session()
    endpoint_url = settings.S3_PRESIGNED_ENDPOINT_URL or settings.S3_ENDPOINT_URL

    config = {
        "service_name": "s3",
        "endpoint_url": endpoint_url,
        "aws_access_key_id": settings.S3_ACCESS_KEY,
        "aws_secret_access_key": settings.S3_SECRET_KEY,
        "region_name": settings.S3_REGION,
    }

    result: dict[str, str] = {}
    async with session.client(**config) as s3:
        for key in keys:
            url = await s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.S3_IMAGES_BUCKET_NAME,
                    "Key": key,
                    "ResponseContentDisposition": "inline",
                },
                ExpiresIn=expires_in,
            )
            result[key] = str(url)

    return result


async def _batch_get_avatar_keys(
    db: AsyncSession,
    user_id: uuid.UUID,
    entity_ids: list[uuid.UUID],
) -> dict[tuple[str, uuid.UUID], str | None]:
    """Batch get avatar keys for accessible avatars.

    For avatars, only the user themselves can access their own avatar.
    Other user IDs are silently excluded (no access).

    Args:
        db: Database session.
        user_id: Authenticated user's UUID.
        entity_ids: List of user IDs to check.

    Returns:
        Dict mapping ("avatar", user_id) to avatar_key (or None if no photo).
    """
    # Only the user's own avatar is accessible
    accessible_ids = [eid for eid in entity_ids if eid == user_id]
    if not accessible_ids:
        return {}

    stmt = select(User.id, User.avatar_key).where(
        User.id.in_(accessible_ids),
        User.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    return {("avatar", row.id): row.avatar_key for row in result}


async def _batch_get_aquarium_keys(
    db: AsyncSession,
    user_id: uuid.UUID,
    entity_ids: list[uuid.UUID],
) -> dict[tuple[str, uuid.UUID], str | None]:
    """Batch get photo keys for accessible aquariums.

    Access: user is owner OR member (via AquariumMember).
    Uses a single SQL query with LEFT JOIN instead of N individual
    check_access() calls.

    The join condition includes user_id to ensure at most one row per
    aquarium (AquariumMember has composite PK aquarium_id+user_id).

    Args:
        db: Database session.
        user_id: Authenticated user's UUID.
        entity_ids: List of aquarium IDs to check.

    Returns:
        Dict mapping ("aquarium", aquarium_id) to photo_key (or None).
    """
    if not entity_ids:
        return {}

    stmt = (
        select(Aquarium.id, Aquarium.photo_key)
        .outerjoin(
            AquariumMember,
            and_(
                Aquarium.id == AquariumMember.aquarium_id,
                AquariumMember.user_id == user_id,
            ),
        )
        .where(
            Aquarium.id.in_(entity_ids),
            Aquarium.deleted_at.is_(None),
            or_(
                Aquarium.owner_id == user_id,
                AquariumMember.user_id == user_id,
            ),
        )
    )
    result = await db.execute(stmt)
    return {("aquarium", row.id): row.photo_key for row in result}


async def _batch_get_fish_keys(
    db: AsyncSession,
    user_id: uuid.UUID,
    entity_ids: list[uuid.UUID],
) -> dict[tuple[str, uuid.UUID], str | None]:
    """Batch get photo keys for accessible fish.

    Access: user is owner or member of the fish's aquarium.
    Joins fish → aquarium → aquarium_members in a single SQL query.

    Args:
        db: Database session.
        user_id: Authenticated user's UUID.
        entity_ids: List of fish IDs to check.

    Returns:
        Dict mapping ("fish", fish_id) to photo_key (or None).
    """
    if not entity_ids:
        return {}

    stmt = (
        select(Fish.id, Fish.photo_key)
        .join(Aquarium, Fish.aquarium_id == Aquarium.id)
        .outerjoin(
            AquariumMember,
            and_(
                Aquarium.id == AquariumMember.aquarium_id,
                AquariumMember.user_id == user_id,
            ),
        )
        .where(
            Fish.id.in_(entity_ids),
            Fish.deleted_at.is_(None),
            Aquarium.deleted_at.is_(None),
            or_(
                Aquarium.owner_id == user_id,
                AquariumMember.user_id == user_id,
            ),
        )
    )
    result = await db.execute(stmt)
    return {("fish", row.id): row.photo_key for row in result}


# --- Main presigned URLs function ---


async def get_presigned_urls(
    db: AsyncSession,
    user_id: uuid.UUID,
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Batch get presigned URLs for entity images with access checking.

    Optimised for batch operations: uses one SQL query per entity_type
    instead of N individual check_access() calls, and one S3 client session
    for all URL generations.

    Response semantics (per architectural document):
    - Entity accessible, has photo_key → {entity_type, entity_id, key, url}
    - Entity accessible, photo_key IS NULL → {entity_type, entity_id, key: None, url: None}
    - Entity NOT accessible (or not found) → item excluded from response

    This allows the client to distinguish "no photo" from "no access".

    Args:
        db: Database session.
        user_id: Authenticated user's UUID (from JWT).
        items: List of dicts with "entity_type" and "entity_id" keys.

    Returns:
        List of dicts with entity_type, entity_id, key, url.
        Items without access are excluded. Items with access but no
        photo have key=None and url=None.
    """
    if not items:
        return []

    # 1. Group items by entity_type for batch DB queries
    grouped: dict[str, list[uuid.UUID]] = {}
    for item in items:
        entity_type = item["entity_type"]
        entity_id = item["entity_id"]
        if entity_type not in VALID_ENTITY_TYPES:
            continue
        grouped.setdefault(str(entity_type), []).append(entity_id)  # type: ignore[arg-type]

    # 2. Batch access check + get photo_key for each entity_type
    #    Result: {(entity_type, entity_id): photo_key_or_None}
    accessible: dict[tuple[str, uuid.UUID], str | None] = {}

    if "avatar" in grouped:
        accessible.update(await _batch_get_avatar_keys(db, user_id, grouped["avatar"]))
    if "aquarium" in grouped:
        accessible.update(await _batch_get_aquarium_keys(db, user_id, grouped["aquarium"]))
    if "fish" in grouped:
        accessible.update(await _batch_get_fish_keys(db, user_id, grouped["fish"]))

    # 3. Collect unique keys that need presigned URLs (skip None)
    keys_to_sign: list[str] = list({
        key for key in accessible.values() if key is not None
    })

    # 4. Generate presigned URLs in one S3 client session
    signed_urls: dict[str, str] = {}
    if keys_to_sign:
        signed_urls = await batch_generate_presigned_urls(keys_to_sign)

    # 5. Build response preserving original item order
    result: list[dict[str, object]] = []
    for item in items:
        et = str(item["entity_type"])
        eid = uuid.UUID(str(item["entity_id"]))
        lookup_key = (et, eid)

        if lookup_key not in accessible:
            # No access or invalid entity_type → exclude from response
            continue

        photo_key = accessible[lookup_key]
        result.append({
            "entity_type": et,
            "entity_id": eid,
            "key": photo_key,
            "url": signed_urls.get(photo_key) if photo_key else None,
        })

    logger.info(
        "get_presigned_urls_completed",
        user_id=str(user_id),
        requested=len(items),
        returned=len(result),
        urls_generated=len(signed_urls),
    )

    return result
