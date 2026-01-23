"""Fish service with business logic for fish management in aquariums."""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.feeding import FeedingEvent
from app.models.fish import Fish
from app.models.species import Species
from app.schemas.fish import FishCreate, FishUpdate
from app.services.aquarium import check_access, get_aquarium
from app.services.gamification import check_achievements

logger = logging.getLogger(__name__)


class FishError(Exception):
    """Base exception for fish errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class FishNotFoundError(FishError):
    """Raised when fish is not found."""

    def __init__(self, fish_id: UUID):
        super().__init__(f"Fish with id '{fish_id}' not found", status_code=404)


class SpeciesNotFoundError(FishError):
    """Raised when species is not found."""

    def __init__(self, species_id: str):
        super().__init__(f"Species with id '{species_id}' not found", status_code=404)


async def add_fish(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
    data: FishCreate,
) -> Fish:
    """Add fish to an aquarium.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID to add fish to.
        user_id: User ID for access check.
        data: Fish creation data.

    Returns:
        Created Fish object with species loaded.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
        SpeciesNotFoundError: If species_id doesn't exist.
    """
    # Check access to aquarium (owner or member)
    await check_access(db, aquarium_id, user_id)

    # Validate species exists
    species_stmt = select(Species).where(Species.id == data.species_id)
    species_result = await db.execute(species_stmt)
    species = species_result.scalar_one_or_none()

    if species is None:
        raise SpeciesNotFoundError(data.species_id)

    # Create fish record
    fish = Fish(
        aquarium_id=aquarium_id,
        species_id=data.species_id,
        quantity=data.quantity,
        custom_name=data.custom_name,
        added_via=data.added_via,
    )
    db.add(fish)
    await db.commit()
    await db.refresh(fish)

    # Load species relationship
    fish.species = species

    logger.info(
        f"Added fish '{fish.id}' (species: {data.species_id}, via: {data.added_via}) "
        f"to aquarium '{aquarium_id}' by user '{user_id}'"
    )

    # Check achievements for aquarium owner
    try:
        aquarium = await get_aquarium(db, aquarium_id, user_id)
        await check_achievements(db, aquarium.owner_id)
    except Exception as e:
        logger.error(f"Failed to check achievements after adding fish: {e}")

    return fish


async def list_fish(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> list[Fish]:
    """List all fish in an aquarium.

    Returns fish that are not soft-deleted, sorted by created_at.
    Eagerly loads species relationship.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Returns:
        List of Fish objects with species loaded.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    # Check access to aquarium
    await check_access(db, aquarium_id, user_id)

    stmt = (
        select(Fish)
        .where(Fish.aquarium_id == aquarium_id)
        .where(Fish.deleted_at.is_(None))
        .options(selectinload(Fish.species))
        .order_by(Fish.created_at)
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_fish(
    db: AsyncSession,
    fish_id: UUID,
    user_id: UUID,
) -> Fish:
    """Get fish by ID with access check.

    Args:
        db: Database session.
        fish_id: Fish ID.
        user_id: User ID for access check.

    Returns:
        Fish object with species loaded.

    Raises:
        FishNotFoundError: If fish not found or deleted.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    stmt = (
        select(Fish)
        .where(Fish.id == fish_id)
        .where(Fish.deleted_at.is_(None))
        .options(selectinload(Fish.species))
    )

    result = await db.execute(stmt)
    fish = result.scalar_one_or_none()

    if fish is None:
        raise FishNotFoundError(fish_id)

    # Check access through aquarium
    await check_access(db, fish.aquarium_id, user_id)

    return fish


async def update_fish(
    db: AsyncSession,
    fish_id: UUID,
    user_id: UUID,
    data: FishUpdate,
) -> Fish:
    """Update a fish. Partial update of quantity and custom_name.

    Args:
        db: Database session.
        fish_id: Fish ID.
        user_id: User ID for access check.
        data: Partial update data.

    Returns:
        Updated Fish object with species loaded.

    Raises:
        FishNotFoundError: If fish not found or deleted.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    # Get fish with access check
    fish = await get_fish(db, fish_id, user_id)

    # Apply partial update
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(fish, field, value)

    # updated_at is handled by TimestampMixin onupdate
    await db.commit()
    await db.refresh(fish)

    # Reload species relationship
    stmt = select(Fish).where(Fish.id == fish_id).options(selectinload(Fish.species))
    result = await db.execute(stmt)
    fish = result.scalar_one()

    logger.info(f"Updated fish '{fish_id}' by user '{user_id}'")
    return fish


async def remove_fish(
    db: AsyncSession,
    fish_id: UUID,
    user_id: UUID,
) -> None:
    """Soft delete a fish.

    Args:
        db: Database session.
        fish_id: Fish ID.
        user_id: User ID for access check.

    Raises:
        FishNotFoundError: If fish not found or already deleted.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    # Get fish with access check
    fish = await get_fish(db, fish_id, user_id)

    # Soft delete
    fish.deleted_at = datetime.now(UTC)

    # Cascade delete: remove all feeding events for this fish
    delete_stmt = select(FeedingEvent).where(FeedingEvent.fish_id == fish_id)
    events_result = await db.execute(delete_stmt)
    events_to_delete = events_result.scalars().all()
    for event in events_to_delete:
        await db.delete(event)

    await db.commit()

    if events_to_delete:
        logger.info(
            f"Cascade deleted {len(events_to_delete)} feeding events "
            f"for fish '{fish_id}'"
        )

    logger.info(
        f"Soft deleted fish '{fish_id}' from aquarium '{fish.aquarium_id}' "
        f"by user '{user_id}'"
    )


async def get_fish_by_species(
    db: AsyncSession,
    aquarium_id: UUID,
    species_id: str,
) -> Fish | None:
    """Find fish by species in an aquarium.

    Used for AI scan deduplication to check if fish of a species
    already exists in the aquarium.

    Note: This function does not perform access check as it's intended
    for internal use during AI scan processing.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        species_id: Species ID to search for.

    Returns:
        Fish object if found, None otherwise.
    """
    stmt = (
        select(Fish)
        .where(Fish.aquarium_id == aquarium_id)
        .where(Fish.species_id == species_id)
        .where(Fish.deleted_at.is_(None))
        .options(selectinload(Fish.species))
    )

    result = await db.execute(stmt)
    return result.scalar_one_or_none()
