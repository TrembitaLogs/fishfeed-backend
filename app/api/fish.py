"""Fish API endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentActiveUser
from app.schemas.fish import FishCreate, FishResponse, FishUpdate
from app.services.aquarium import AquariumAccessDeniedError, AquariumNotFoundError
from app.services.fish import (
    FishNotFoundError,
    SpeciesNotFoundError,
    add_fish,
    get_fish,
    list_fish,
    remove_fish,
    update_fish,
)

router = APIRouter(tags=["Fish"])


@router.get(
    "/aquariums/{aquarium_id}/fish",
    response_model=list[FishResponse],
    summary="List fish in aquarium",
    responses={
        200: {"description": "List of fish"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium not found"},
    },
)
async def list_aquarium_fish(
    aquarium_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> list[FishResponse]:
    """Get all fish in an aquarium."""
    try:
        fish_list = await list_fish(db, aquarium_id, current_user.id)
        return [FishResponse.model_validate(f) for f in fish_list]
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.post(
    "/aquariums/{aquarium_id}/fish",
    response_model=FishResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add fish to aquarium",
    responses={
        201: {"description": "Fish added"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Aquarium or species not found"},
        422: {"description": "Validation error"},
    },
)
async def add_fish_to_aquarium(
    aquarium_id: UUID,
    data: FishCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> FishResponse:
    """Add a fish to an aquarium."""
    try:
        fish = await add_fish(db, aquarium_id, current_user.id, data)
        return FishResponse.model_validate(fish)
    except AquariumNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except SpeciesNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.get(
    "/fish/{fish_id}",
    response_model=FishResponse,
    summary="Get fish details",
    responses={
        200: {"description": "Fish details"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Fish not found"},
    },
)
async def get_fish_details(
    fish_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> FishResponse:
    """Get details of a specific fish."""
    try:
        fish = await get_fish(db, fish_id, current_user.id)
        return FishResponse.model_validate(fish)
    except FishNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.put(
    "/fish/{fish_id}",
    response_model=FishResponse,
    summary="Update fish",
    responses={
        200: {"description": "Fish updated"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Fish not found"},
        422: {"description": "Validation error"},
    },
)
async def update_fish_details(
    fish_id: UUID,
    data: FishUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> FishResponse:
    """Update a fish's details."""
    try:
        fish = await update_fish(db, fish_id, current_user.id, data)
        return FishResponse.model_validate(fish)
    except FishNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@router.delete(
    "/fish/{fish_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete fish",
    responses={
        204: {"description": "Fish deleted"},
        401: {"description": "Not authenticated"},
        403: {"description": "Access denied"},
        404: {"description": "Fish not found"},
    },
)
async def delete_fish(
    fish_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentActiveUser,
) -> None:
    """Soft delete a fish from an aquarium."""
    try:
        await remove_fish(db, fish_id, current_user.id)
    except FishNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
    except AquariumAccessDeniedError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
