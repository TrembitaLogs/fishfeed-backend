"""AI fish recognition API endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import (
    CurrentActiveUser,
    RateLimitCheck,
)
from app.models.user import User
from app.redis import get_redis
from app.schemas.ai import (
    ScanConfirmRequest,
    ScanRequest,
    ScanResponse,
    ScansRemainingResponse,
)
from app.services.ai import (
    AIServiceError,
    ScanAccessDeniedError,
    ScanLimitExceededError,
    ScanNotFoundError,
    SpeciesNotFoundError,
    confirm_species,
    get_remaining_scans,
    get_scan,
    get_scan_history,
    scan_image,
)
from app.services.ai_provider import AIProviderError
from app.services.image_processing import ImageProcessingError
from app.services.rate_limiter import AIRateLimiter

router = APIRouter(prefix="/ai", tags=["AI Fish Recognition"])


async def _process_scan(
    db: AsyncSession,
    redis: Redis,
    current_user: User,
    image_base64: str | None = None,
    image_file: UploadFile | None = None,
) -> ScanResponse:
    """Common scan processing logic."""
    try:
        result = await scan_image(
            db=db,
            user_id=current_user.id,
            image_base64=image_base64,
            image_file=image_file,
        )

        # Increment rate limit counter after successful scan
        is_premium = current_user.subscription_status != "free"
        if not is_premium:
            limiter = AIRateLimiter(redis)
            await limiter.increment_scan_count(current_user.id)

        return result

    except ScanLimitExceededError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except ImageProcessingError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid image: {e.message}",
        ) from None
    except AIProviderError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"AI service unavailable: {e.message}",
        ) from None
    except AIServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.post(
    "/scan",
    response_model=ScanResponse,
    status_code=status.HTTP_200_OK,
    summary="Scan image for fish species (base64)",
    responses={
        200: {"description": "Scan completed successfully"},
        400: {"description": "Invalid image or no image provided"},
        401: {"description": "Not authenticated"},
        402: {"description": "No remaining scans (payment required)"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "AI service unavailable"},
    },
)
async def scan_fish_image(
    request: ScanRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    current_user: CurrentActiveUser,
    rate_limit: RateLimitCheck,
) -> ScanResponse:
    """Scan an image for fish species recognition using base64 encoded image.

    Send the image as a base64 encoded string in the request body.
    Rate limited for free users.

    Returns the detected species with confidence score and alternatives.
    """
    if not request.image_base64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No image provided. Provide image_base64 in request body.",
        )

    return await _process_scan(
        db=db,
        redis=redis,
        current_user=current_user,
        image_base64=request.image_base64,
    )


@router.post(
    "/scan/upload",
    response_model=ScanResponse,
    status_code=status.HTTP_200_OK,
    summary="Scan image for fish species (file upload)",
    responses={
        200: {"description": "Scan completed successfully"},
        400: {"description": "Invalid image or no image provided"},
        401: {"description": "Not authenticated"},
        402: {"description": "No remaining scans (payment required)"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "AI service unavailable"},
    },
)
async def scan_fish_image_upload(
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    current_user: CurrentActiveUser,
    rate_limit: RateLimitCheck,
) -> ScanResponse:
    """Scan an image for fish species recognition using file upload.

    Upload the image as a multipart/form-data file.
    Rate limited for free users.

    Returns the detected species with confidence score and alternatives.
    """
    if not file or not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No image file provided.",
        )

    return await _process_scan(
        db=db,
        redis=redis,
        current_user=current_user,
        image_file=file,
    )


@router.get(
    "/scans/remaining",
    response_model=ScansRemainingResponse,
    status_code=status.HTTP_200_OK,
    summary="Get remaining scan count",
    responses={
        200: {"description": "Remaining scans retrieved"},
        401: {"description": "Not authenticated"},
    },
)
async def get_scans_remaining(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> ScansRemainingResponse:
    """Get the number of remaining AI scans for the current user.

    Premium users have unlimited scans (indicated by -1).
    Free users have a limited number of total scans.
    """
    is_premium = current_user.subscription_status != "free"
    remaining = await get_remaining_scans(db, current_user.id)

    return ScansRemainingResponse(
        scans_remaining=remaining if remaining >= 0 else 999999,
        is_premium=is_premium,
    )


@router.post(
    "/scans/{scan_id}/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Confirm species identification",
    responses={
        204: {"description": "Species confirmed successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied to this scan"},
        404: {"description": "Scan or species not found"},
    },
)
async def confirm_scan_species(
    scan_id: UUID,
    request: ScanConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Confirm or correct the species identification for a scan.

    Used for model improvement by collecting user feedback.
    The user must be the owner of the scan.
    """
    try:
        await confirm_species(
            db=db,
            scan_id=scan_id,
            user_id=current_user.id,
            species_id=request.species_id,
        )
    except ScanNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except ScanAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except SpeciesNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.get(
    "/scans/history",
    response_model=list[ScanResponse],
    status_code=status.HTTP_200_OK,
    summary="Get scan history",
    responses={
        200: {"description": "Scan history retrieved"},
        401: {"description": "Not authenticated"},
    },
)
async def get_user_scan_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
    limit: int = 10,
    offset: int = 0,
) -> list[ScanResponse]:
    """Get the scan history for the current user.

    Returns scans ordered by creation date (newest first).
    Supports pagination with limit and offset parameters.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    if offset < 0:
        offset = 0

    return await get_scan_history(
        db=db,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/scans/{scan_id}",
    response_model=ScanResponse,
    status_code=status.HTTP_200_OK,
    summary="Get specific scan",
    responses={
        200: {"description": "Scan retrieved"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied to this scan"},
        404: {"description": "Scan not found"},
    },
)
async def get_scan_by_id(
    scan_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> ScanResponse:
    """Get a specific scan by ID.

    The user must be the owner of the scan.
    """
    try:
        return await get_scan(
            db=db,
            scan_id=scan_id,
            user_id=current_user.id,
        )
    except ScanNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except ScanAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
