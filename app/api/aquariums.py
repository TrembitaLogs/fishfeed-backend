"""Aquarium API endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.aquarium import (
    AquariumCreate,
    AquariumResponse,
    AquariumUpdate,
    AquariumWithFish,
)
from app.schemas.feeding import ScheduleResponse
from app.services.aquarium import (
    AquariumAccessDeniedError,
    AquariumNotFoundError,
    AquariumOwnerRequiredError,
    create_aquarium,
    delete_aquarium,
    get_aquarium_with_fish,
    list_user_aquariums,
    update_aquarium,
)
from app.services.feeding import get_schedule

router = APIRouter(prefix="/aquariums", tags=["Aquariums"])


@router.get(
    "",
    response_model=list[AquariumResponse],
    summary="List user's aquariums",
    responses={
        200: {"description": "List of aquariums"},
        401: {"description": "Not authenticated"},
    },
)
async def list_aquariums(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> list[AquariumResponse]:
    """Get all aquariums accessible to the current user.

    Returns aquariums where user is owner or member.
    """
    aquariums = await list_user_aquariums(db, current_user.id)
    return [AquariumResponse.model_validate(a) for a in aquariums]


@router.post(
    "",
    response_model=AquariumResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create aquarium",
    responses={
        201: {"description": "Aquarium created"},
        401: {"description": "Not authenticated"},
        422: {"description": "Validation error"},
    },
)
async def create_new_aquarium(
    data: AquariumCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> AquariumResponse:
    """Create a new aquarium for the current user."""
    aquarium = await create_aquarium(db, current_user.id, data)
    return AquariumResponse.model_validate(aquarium)


@router.get(
    "/{aquarium_id}",
    response_model=AquariumWithFish,
    summary="Get aquarium details",
    responses={
        200: {"description": "Aquarium details with fish and schedule"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def get_aquarium_details(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> AquariumWithFish:
    """Get aquarium details including fish and feeding schedule."""
    try:
        aquarium = await get_aquarium_with_fish(db, aquarium_id, current_user.id)
        schedule = await get_schedule(db, aquarium_id, current_user.id)

        response = AquariumWithFish.model_validate(aquarium)
        if schedule:
            response.schedule = ScheduleResponse.model_validate(schedule)
        return response
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.put(
    "/{aquarium_id}",
    response_model=AquariumResponse,
    summary="Update aquarium",
    responses={
        200: {"description": "Aquarium updated"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied or owner required"},
        404: {"description": "Aquarium not found"},
        422: {"description": "Validation error"},
    },
)
async def update_aquarium_details(
    aquarium_id: UUID,
    data: AquariumUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> AquariumResponse:
    """Update an aquarium. Only owner can update."""
    try:
        aquarium = await update_aquarium(db, aquarium_id, current_user.id, data)
        return AquariumResponse.model_validate(aquarium)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except (AquariumAccessDeniedError, AquariumOwnerRequiredError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.delete(
    "/{aquarium_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete aquarium",
    responses={
        204: {"description": "Aquarium deleted"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied or owner required"},
        404: {"description": "Aquarium not found"},
    },
)
async def delete_aquarium_endpoint(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Soft delete an aquarium. Only owner can delete."""
    try:
        await delete_aquarium(db, aquarium_id, current_user.id)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except (AquariumAccessDeniedError, AquariumOwnerRequiredError) as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
