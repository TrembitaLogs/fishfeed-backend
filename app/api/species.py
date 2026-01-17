"""Species API endpoints for fish species database."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CurrentAdmin
from app.redis import get_redis
from app.schemas.species import (
    CareLevel,
    SpeciesCreate,
    SpeciesListResponse,
    SpeciesResponse,
    SpeciesSearchQuery,
    SpeciesUpdate,
    WaterType,
)
from app.services.species import (
    SpeciesAlreadyExistsError,
    SpeciesNotFoundError,
    create_species,
    delete_species,
    get_popular_species,
    get_species_cached,
    list_species,
    search_species,
    update_species,
)

router = APIRouter(prefix="/species", tags=["Species"])
admin_router = APIRouter(prefix="/admin/species", tags=["Admin Species"])


@router.get(
    "",
    response_model=SpeciesListResponse,
    status_code=status.HTTP_200_OK,
    summary="List all species",
    responses={
        200: {"description": "Paginated list of species"},
    },
)
async def list_species_endpoint(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    page: int = Query(default=1, ge=1, description="Page number"),
    per_page: int = Query(default=20, ge=1, le=100, description="Items per page"),
    care_level: CareLevel | None = Query(default=None, description="Filter by care level"),
    water_type: WaterType | None = Query(default=None, description="Filter by water type"),
) -> SpeciesListResponse:
    """Get a paginated list of fish species.

    Supports filtering by care_level and water_type.
    Results are ordered by common name and cached for 1 hour.
    """
    filters = SpeciesSearchQuery(care_level=care_level, water_type=water_type)
    return await list_species(db, page=page, per_page=per_page, filters=filters, redis=redis)


@router.get(
    "/search",
    response_model=list[SpeciesResponse],
    status_code=status.HTTP_200_OK,
    summary="Search species",
    responses={
        200: {"description": "List of matching species"},
        422: {"description": "Query too short (minimum 2 characters)"},
    },
)
async def search_species_endpoint(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    q: str = Query(..., min_length=2, max_length=100, description="Search query"),
) -> list[SpeciesResponse]:
    """Search species by name using full-text search.

    Searches both common_name and scientific_name fields.
    Minimum query length is 2 characters. Results cached for 30 minutes.
    """
    return await search_species(db, query=q, redis=redis)


@router.get(
    "/popular",
    response_model=list[SpeciesResponse],
    status_code=status.HTTP_200_OK,
    summary="Get popular species",
    responses={
        200: {"description": "List of popular species for onboarding"},
    },
)
async def get_popular_species_endpoint(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> list[SpeciesResponse]:
    """Get top 20 popular species for onboarding.

    Returns a curated list of common aquarium fish species
    suitable for beginners and hobbyists. Cached for 1 hour.
    """
    return await get_popular_species(db, limit=20, redis=redis)


@router.get(
    "/{species_id}",
    response_model=SpeciesResponse,
    status_code=status.HTTP_200_OK,
    summary="Get species by ID",
    responses={
        200: {"description": "Species details"},
        404: {"description": "Species not found"},
    },
)
async def get_species_endpoint(
    species_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> SpeciesResponse:
    """Get detailed information about a specific fish species. Cached for 24 hours."""
    try:
        return await get_species_cached(db, species_id, redis=redis)
    except SpeciesNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


# Admin endpoints


@admin_router.post(
    "",
    response_model=SpeciesResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new species",
    responses={
        201: {"description": "Species created successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Admin privileges required"},
        409: {"description": "Species with this ID already exists"},
    },
)
async def create_species_endpoint(
    data: SpeciesCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    admin: CurrentAdmin,
) -> SpeciesResponse:
    """Create a new fish species entry.

    Requires admin privileges. Invalidates species list cache.
    """
    try:
        species = await create_species(db, data, redis=redis)
        return SpeciesResponse.model_validate(species)
    except SpeciesAlreadyExistsError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@admin_router.put(
    "/{species_id}",
    response_model=SpeciesResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a species",
    responses={
        200: {"description": "Species updated successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Species not found"},
    },
)
async def update_species_endpoint(
    species_id: str,
    data: SpeciesUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    admin: CurrentAdmin,
) -> SpeciesResponse:
    """Update an existing fish species.

    Requires admin privileges. All fields are optional. Invalidates cache.
    """
    try:
        species = await update_species(db, species_id, data, redis=redis)
        return SpeciesResponse.model_validate(species)
    except SpeciesNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None


@admin_router.delete(
    "/{species_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a species",
    responses={
        204: {"description": "Species deleted successfully"},
        401: {"description": "Not authenticated"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Species not found"},
    },
)
async def delete_species_endpoint(
    species_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    admin: CurrentAdmin,
) -> None:
    """Delete a fish species from the database.

    Requires admin privileges. Invalidates all species cache.
    """
    try:
        await delete_species(db, species_id, redis=redis)
    except SpeciesNotFoundError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
