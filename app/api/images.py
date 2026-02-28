"""Image upload and presigned URL API endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser, ImageUploadRateLimitCheck
from app.schemas.image import (
    PresignedUrlResult,
    PresignedUrlsRequest,
    PresignedUrlsResponse,
    UploadResponse,
)
from app.services.image_service import (
    AccessDeniedError,
    EntityNotFoundError,
    FileTooLargeError,
    ImageServiceError,
    UnsupportedMediaTypeError,
)
from app.services.image_service import (
    get_presigned_urls as svc_get_presigned_urls,
)
from app.services.image_service import (
    upload_image as svc_upload_image,
)

router = APIRouter(prefix="/images", tags=["Images"])

_VALID_ENTITY_TYPES = frozenset({"aquarium", "fish", "avatar"})
_MAX_PRESIGNED_URL_ITEMS = 50


@router.post(
    "/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadResponse,
    summary="Upload entity image",
    responses={
        201: {"description": "Image uploaded successfully"},
        400: {"description": "Invalid entity_type or entity_id"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied to entity"},
        404: {"description": "Entity not found"},
        413: {"description": "File size exceeds limit"},
        415: {"description": "Unsupported image format"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def upload_image(
    file: UploadFile = File(..., description="Image file (JPEG, PNG, or WebP)"),
    entity_type: str = Form(..., description="Entity type: aquarium, fish, or avatar"),
    entity_id: str = Form(..., description="UUID of the entity"),
    current_user: CurrentActiveUser = None,  # type: ignore[assignment]  # noqa: RUF013
    _rate_limit: ImageUploadRateLimitCheck = None,  # noqa: RUF013
    db: AsyncSession = Depends(get_db),
) -> UploadResponse:
    """Upload an image for an entity (aquarium, fish, or avatar).

    Accepts multipart/form-data with the image file, entity type, and entity ID.
    For avatar uploads, entity_id should match the authenticated user's ID.
    """
    if entity_type not in _VALID_ENTITY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid entity_type: '{entity_type}'. Must be one of: aquarium, fish, avatar",
        )

    try:
        entity_uuid = UUID(entity_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid entity_id: '{entity_id}'. Must be a valid UUID",
        ) from None

    content = await file.read()

    try:
        key = await svc_upload_image(
            db=db,
            user_id=current_user.id,
            entity_type=entity_type,
            entity_id=entity_uuid,
            file_content=content,
        )
    except EntityNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=e.message) from None
    except AccessDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message) from None
    except UnsupportedMediaTypeError as e:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=e.message
        ) from None
    except FileTooLargeError as e:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=e.message
        ) from None
    except ImageServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from None

    return UploadResponse(key=key, entity_type=entity_type, entity_id=entity_id)


@router.post(
    "/urls",
    response_model=PresignedUrlsResponse,
    summary="Get presigned URLs for entity images",
    responses={
        200: {"description": "Presigned URLs generated successfully"},
        400: {"description": "Too many items in request"},
        401: {"description": "Not authenticated"},
    },
)
async def get_presigned_urls(
    request: PresignedUrlsRequest,
    current_user: CurrentActiveUser = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
) -> PresignedUrlsResponse:
    """Get batch presigned GET URLs for entity images.

    Returns presigned URLs for accessible entities. Entities without access
    are excluded from the response. Entities with no photo return null key and url.
    Maximum 50 items per request.
    """
    if len(request.items) > _MAX_PRESIGNED_URL_ITEMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {_MAX_PRESIGNED_URL_ITEMS} items per request",
        )

    items_for_service: list[dict[str, object]] = [
        {"entity_type": item.entity_type, "entity_id": item.entity_id}
        for item in request.items
    ]

    result = await svc_get_presigned_urls(
        db=db,
        user_id=current_user.id,
        items=items_for_service,
    )

    return PresignedUrlsResponse(
        items=[
            PresignedUrlResult(
                entity_type=str(r["entity_type"]),
                entity_id=r["entity_id"],  # type: ignore[arg-type]
                key=r["key"],  # type: ignore[arg-type]
                url=r["url"],  # type: ignore[arg-type]
            )
            for r in result
        ],
    )
