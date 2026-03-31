"""AI fish recognition service with business logic.

This module provides the main business logic for AI-powered fish species
recognition, including scan limits, deduplication, and result logging.
"""

import time
from uuid import UUID

import structlog
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIScan
from app.models.species import Species
from app.models.user import User
from app.schemas.ai import AlternativeSpecies, ScanResponse
from app.services.ai_provider import (
    AIProviderError,
    ClassificationResult,
    Prediction,
    classify_with_fallback,
)
from app.services.image_processing import (
    calculate_image_hash,
    decode_base64_image,
    preprocess_for_ai,
    process_upload_file,
)
from app.services.storage import S3StorageService, StorageError, get_storage_service

logger = structlog.get_logger(__name__)


class AIServiceError(Exception):
    """Base exception for AI service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ScanLimitExceededError(AIServiceError):
    """Raised when user has no remaining scans."""

    def __init__(self) -> None:
        super().__init__(
            "Free scan limit exceeded. Upgrade to premium for unlimited scans.",
            status_code=402,
        )


class ScanNotFoundError(AIServiceError):
    """Raised when scan is not found."""

    def __init__(self, scan_id: UUID) -> None:
        super().__init__(f"Scan with id '{scan_id}' not found", status_code=404)


class ScanAccessDeniedError(AIServiceError):
    """Raised when user tries to access another user's scan."""

    def __init__(self) -> None:
        super().__init__("Access denied to this scan", status_code=403)


class SpeciesNotFoundError(AIServiceError):
    """Raised when species is not found."""

    def __init__(self, species_id: str) -> None:
        super().__init__(f"Species with id '{species_id}' not found", status_code=404)


async def get_remaining_scans(db: AsyncSession, user_id: UUID) -> int:
    """Get remaining AI scans for user.

    Args:
        db: Database session.
        user_id: User identifier.

    Returns:
        Number of remaining scans. Returns -1 for premium users (unlimited).
    """
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        return 0

    # Premium users have unlimited scans
    if user.subscription_status != "free":
        return -1

    return user.free_ai_scans_remaining


async def _get_user(db: AsyncSession, user_id: UUID) -> User | None:
    """Get user by ID."""
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _match_label_to_species(
    db: AsyncSession,
    label: str,
) -> Species | None:
    """Match AI prediction label to species in database.

    Performs case-insensitive search on common_name and scientific_name.

    Args:
        db: Database session.
        label: AI prediction label.

    Returns:
        Matching Species or None if not found.
    """
    label_lower = label.lower().strip()

    # Try exact match on common_name first
    stmt = select(Species).where(Species.common_name.ilike(label_lower))
    result = await db.execute(stmt)
    species = result.scalar_one_or_none()
    if species:
        return species

    # Try scientific_name
    stmt = select(Species).where(Species.scientific_name.ilike(label_lower))
    result = await db.execute(stmt)
    species = result.scalar_one_or_none()
    if species:
        return species

    # Try partial match on common_name
    stmt = select(Species).where(Species.common_name.ilike(f"%{label_lower}%"))
    result = await db.execute(stmt)
    species = result.scalar_one_or_none()
    if species:
        return species

    return None


async def _map_predictions_to_species(
    db: AsyncSession,
    predictions: list[Prediction],
) -> tuple[Species | None, float, list[dict]]:
    """Map AI predictions to species in database.

    Args:
        db: Database session.
        predictions: List of AI predictions sorted by confidence.

    Returns:
        Tuple of (primary_species, confidence, alternatives_data).
    """
    primary_species: Species | None = None
    primary_confidence: float = 0.0
    alternatives: list[dict] = []

    for prediction in predictions:
        species = await _match_label_to_species(db, prediction.label)
        if species is None:
            continue

        if primary_species is None:
            primary_species = species
            primary_confidence = prediction.confidence
        else:
            alternatives.append({
                "species_id": species.id,
                "species_name": species.common_name,
                "confidence": prediction.confidence,
            })

            # Limit to top 3 alternatives
            if len(alternatives) >= 3:
                break

    return primary_species, primary_confidence, alternatives


async def _find_cached_scan(
    db: AsyncSession,
    user_id: UUID,
    image_hash: str,
) -> AIScan | None:
    """Find existing scan with same image hash for user.

    Args:
        db: Database session.
        user_id: User identifier.
        image_hash: SHA-256 hash of preprocessed image.

    Returns:
        Existing AIScan or None if not found.
    """
    stmt = (
        select(AIScan)
        .where(AIScan.user_id == user_id)
        .where(AIScan.image_hash == image_hash)
        .order_by(AIScan.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _build_scan_response(
    db: AsyncSession,
    scan: AIScan,
    scans_remaining: int,
) -> ScanResponse:
    """Build ScanResponse from AIScan model.

    Args:
        db: Database session.
        scan: AIScan model instance.
        scans_remaining: Remaining scans for user.

    Returns:
        ScanResponse schema.
    """
    species_name: str | None = None
    if scan.detected_species_id:
        stmt = select(Species).where(Species.id == scan.detected_species_id)
        result = await db.execute(stmt)
        species = result.scalar_one_or_none()
        if species:
            species_name = species.common_name

    alternatives: list[AlternativeSpecies] = []
    if scan.alternatives:
        for alt in scan.alternatives:
            alternatives.append(
                AlternativeSpecies(
                    species_id=alt["species_id"],
                    species_name=alt["species_name"],
                    confidence=alt["confidence"],
                )
            )

    return ScanResponse(
        scan_id=scan.id,
        species_id=scan.detected_species_id,
        species_name=species_name,
        confidence=scan.confidence or 0.0,
        alternatives=alternatives,
        scans_remaining=max(scans_remaining, 0),
        image_url=scan.image_url,
    )


async def scan_image(
    db: AsyncSession,
    user_id: UUID,
    image_base64: str | None = None,
    image_file: UploadFile | None = None,
    storage: S3StorageService | None = None,
) -> ScanResponse:
    """Scan image for fish species recognition.

    Processes image through AI provider and logs result to database.
    Handles scan limits for free users and deduplication via image hash.

    Args:
        db: Database session.
        user_id: User identifier.
        image_base64: Base64 encoded image data.
        image_file: Uploaded image file.

    Returns:
        ScanResponse with detection results.

    Raises:
        ScanLimitExceededError: If free user has no remaining scans.
        ImageProcessingError: If image processing fails.
        AIProviderError: If AI classification fails.
    """
    start_time = time.monotonic()

    # Get user and check limits
    user = await _get_user(db, user_id)
    if user is None:
        raise AIServiceError("User not found", status_code=404)

    is_premium = user.subscription_status != "free"
    scans_remaining = -1 if is_premium else user.free_ai_scans_remaining

    if not is_premium and scans_remaining <= 0:
        raise ScanLimitExceededError()

    # Process image
    if image_base64:
        pil_image = decode_base64_image(image_base64)
    elif image_file:
        pil_image = await process_upload_file(image_file)
    else:
        raise AIServiceError("No image provided", status_code=400)

    # Preprocess for AI
    preprocessed_bytes = preprocess_for_ai(pil_image)
    image_hash = calculate_image_hash(preprocessed_bytes)

    # Check for cached result (deduplication)
    cached_scan = await _find_cached_scan(db, user_id, image_hash)
    if cached_scan:
        logger.info("Returning cached scan", scan_id=cached_scan.id, user_id=user_id)
        return await _build_scan_response(db, cached_scan, scans_remaining)

    # Call AI provider
    try:
        classification_result: ClassificationResult = await classify_with_fallback(
            preprocessed_bytes
        )
    except AIProviderError as e:
        logger.error("AI classification failed", user_id=user_id, error=str(e))
        raise

    # Map predictions to species
    primary_species, confidence, alternatives = await _map_predictions_to_species(
        db, classification_result.predictions
    )

    processing_time_ms = int((time.monotonic() - start_time) * 1000)

    # Upload image to S3 storage
    image_url: str | None = None
    if storage is None:
        storage = get_storage_service()
    try:
        image_url = await storage.upload_image(preprocessed_bytes, image_hash)
    except StorageError as e:
        logger.warning("Failed to upload image to S3", error_message=e.message)
        # Continue without image URL - scan is still valid

    # Create scan record
    scan = AIScan(
        user_id=user_id,
        image_hash=image_hash,
        image_url=image_url,
        detected_species_id=primary_species.id if primary_species else None,
        confidence=confidence if primary_species else None,
        alternatives=alternatives if alternatives else None,
        processing_time_ms=processing_time_ms,
    )
    db.add(scan)

    # Decrease remaining scans for free users
    if not is_premium:
        user.free_ai_scans_remaining -= 1
        scans_remaining = user.free_ai_scans_remaining

    await db.flush()
    await db.refresh(scan)

    logger.info(
        "Scan completed",
        scan_id=scan.id,
        user_id=user_id,
        species_id=primary_species.id if primary_species else None,
        confidence=round(confidence, 2),
        processing_time_ms=processing_time_ms,
    )

    return await _build_scan_response(db, scan, scans_remaining)


async def confirm_species(
    db: AsyncSession,
    scan_id: UUID,
    user_id: UUID,
    species_id: str,
) -> None:
    """Confirm or correct species identification for a scan.

    Used for model improvement by collecting user feedback.

    Args:
        db: Database session.
        scan_id: Scan identifier.
        user_id: User identifier (for ownership check).
        species_id: User-confirmed species ID.

    Raises:
        ScanNotFoundError: If scan not found.
        ScanAccessDeniedError: If scan belongs to another user.
        SpeciesNotFoundError: If species not found.
    """
    # Get scan
    stmt = select(AIScan).where(AIScan.id == scan_id)
    result = await db.execute(stmt)
    scan = result.scalar_one_or_none()

    if scan is None:
        raise ScanNotFoundError(scan_id)

    if scan.user_id != user_id:
        raise ScanAccessDeniedError()

    # Verify species exists
    species_stmt = select(Species).where(Species.id == species_id)
    result = await db.execute(species_stmt)
    species = result.scalar_one_or_none()

    if species is None:
        raise SpeciesNotFoundError(species_id)

    # Update scan with confirmation
    scan.confirmed_species_id = species_id
    scan.was_corrected = scan.detected_species_id != species_id

    await db.flush()

    logger.info(
        "Scan confirmed",
        scan_id=scan_id,
        user_id=user_id,
        species_id=species_id,
        was_corrected=scan.was_corrected,
    )


async def get_scan_history(
    db: AsyncSession,
    user_id: UUID,
    limit: int = 10,
    offset: int = 0,
) -> list[ScanResponse]:
    """Get user's scan history.

    Args:
        db: Database session.
        user_id: User identifier.
        limit: Maximum number of scans to return.
        offset: Number of scans to skip.

    Returns:
        List of ScanResponse objects ordered by creation date (newest first).
    """
    scans_remaining = await get_remaining_scans(db, user_id)

    stmt = (
        select(AIScan)
        .where(AIScan.user_id == user_id)
        .order_by(AIScan.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    scans = result.scalars().all()

    responses: list[ScanResponse] = []
    for scan in scans:
        response = await _build_scan_response(db, scan, scans_remaining)
        responses.append(response)

    return responses


async def get_scan(
    db: AsyncSession,
    scan_id: UUID,
    user_id: UUID,
) -> ScanResponse:
    """Get a specific scan by ID.

    Args:
        db: Database session.
        scan_id: Scan identifier.
        user_id: User identifier (for ownership check).

    Returns:
        ScanResponse for the scan.

    Raises:
        ScanNotFoundError: If scan not found.
        ScanAccessDeniedError: If scan belongs to another user.
    """
    stmt = select(AIScan).where(AIScan.id == scan_id)
    result = await db.execute(stmt)
    scan = result.scalar_one_or_none()

    if scan is None:
        raise ScanNotFoundError(scan_id)

    if scan.user_id != user_id:
        raise ScanAccessDeniedError()

    scans_remaining = await get_remaining_scans(db, user_id)
    return await _build_scan_response(db, scan, scans_remaining)
