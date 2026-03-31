"""Aquarium service with business logic for aquarium management."""

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import case, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.aquarium import Aquarium, AquariumMember
from app.models.fish import Fish
from app.schemas.aquarium import AquariumCreate, AquariumUpdate

logger = structlog.get_logger(__name__)


class AquariumError(Exception):
    """Base exception for aquarium errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class AquariumNotFoundError(AquariumError):
    """Raised when aquarium is not found."""

    def __init__(self, aquarium_id: UUID):
        super().__init__(f"Aquarium with id '{aquarium_id}' not found", status_code=404)


class AquariumAccessDeniedError(AquariumError):
    """Raised when user doesn't have access to aquarium."""

    def __init__(self, aquarium_id: UUID):
        super().__init__(f"Access denied to aquarium '{aquarium_id}'", status_code=403)


class AquariumOwnerRequiredError(AquariumError):
    """Raised when operation requires owner role."""

    def __init__(self, aquarium_id: UUID):
        super().__init__(
            f"Owner role required for aquarium '{aquarium_id}'", status_code=403
        )


async def check_access(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> tuple[Aquarium, str]:
    """Check if user has access to aquarium and return their role.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID.

    Returns:
        Tuple of (Aquarium, role) where role is 'owner' or 'member'.

    Raises:
        AquariumNotFoundError: If aquarium not found or deleted.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    # Single atomic query: fetch aquarium + membership in one round-trip
    # to avoid TOCTOU race between existence check and access check.
    stmt = (
        select(
            Aquarium,
            case(
                (Aquarium.owner_id == user_id, literal_column("'owner'")),
                else_=AquariumMember.role,
            ).label("resolved_role"),
        )
        .outerjoin(
            AquariumMember,
            (AquariumMember.aquarium_id == Aquarium.id)
            & (AquariumMember.user_id == user_id),
        )
        .where(Aquarium.id == aquarium_id)
        .where(Aquarium.deleted_at.is_(None))
    )
    result = await db.execute(stmt)
    row = result.one_or_none()

    if row is None:
        raise AquariumNotFoundError(aquarium_id)

    aquarium, role = row.tuple()

    if role is None:
        raise AquariumAccessDeniedError(aquarium_id)

    return aquarium, role


async def create_aquarium(
    db: AsyncSession,
    user_id: UUID,
    data: AquariumCreate,
) -> Aquarium:
    """Create a new aquarium for user.

    Args:
        db: Database session.
        user_id: Owner user ID.
        data: Aquarium creation data.

    Returns:
        Created Aquarium object.
    """
    aquarium = Aquarium(
        owner_id=user_id,
        name=data.name,
    )
    db.add(aquarium)
    await db.flush()

    # Add owner as member with 'owner' role
    member = AquariumMember(
        aquarium_id=aquarium.id,
        user_id=user_id,
        role="owner",
    )
    db.add(member)

    await db.flush()
    await db.refresh(aquarium)

    logger.info("Created aquarium", aquarium_id=aquarium.id, user_id=user_id)

    return aquarium


async def list_user_aquariums(
    db: AsyncSession,
    user_id: UUID,
) -> list[Aquarium]:
    """List all aquariums accessible to user.

    Returns aquariums where user is owner OR member.
    Excludes soft-deleted aquariums.
    Sorted by created_at DESC.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        List of Aquarium objects.
    """
    # Get aquariums where user is owner or member
    stmt = (
        select(Aquarium)
        .distinct()
        .outerjoin(AquariumMember, Aquarium.id == AquariumMember.aquarium_id)
        .where(
            Aquarium.deleted_at.is_(None),
            or_(
                Aquarium.owner_id == user_id,
                AquariumMember.user_id == user_id,
            ),
        )
        .order_by(Aquarium.created_at.desc())
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_aquarium(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> Aquarium:
    """Get aquarium by ID with access check.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Returns:
        Aquarium object.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    aquarium, _ = await check_access(db, aquarium_id, user_id)
    return aquarium


async def get_aquarium_with_fish(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> Aquarium:
    """Get aquarium by ID with fish eagerly loaded.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Returns:
        Aquarium object with fish loaded.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    # First check access
    await check_access(db, aquarium_id, user_id)

    # Then load with relationships
    stmt = (
        select(Aquarium)
        .where(Aquarium.id == aquarium_id)
        .where(Aquarium.deleted_at.is_(None))
        .options(selectinload(Aquarium.fish.and_(Fish.deleted_at.is_(None))))
    )
    result = await db.execute(stmt)
    aquarium = result.scalar_one()
    return aquarium


async def update_aquarium(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
    data: AquariumUpdate,
) -> Aquarium:
    """Update an aquarium. Only owner can update.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.
        data: Partial update data.

    Returns:
        Updated Aquarium object.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumOwnerRequiredError: If user is not owner.
    """
    aquarium, role = await check_access(db, aquarium_id, user_id)

    if role != "owner":
        raise AquariumOwnerRequiredError(aquarium_id)

    # Apply partial update
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(aquarium, field, value)

    await db.flush()
    await db.refresh(aquarium)

    logger.info("Updated aquarium", aquarium_id=aquarium_id, user_id=user_id)
    return aquarium


async def delete_aquarium(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> None:
    """Soft delete an aquarium. Only owner can delete.

    Performs cascade soft delete for fish.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumOwnerRequiredError: If user is not owner.
    """
    aquarium, role = await check_access(db, aquarium_id, user_id)

    if role != "owner":
        raise AquariumOwnerRequiredError(aquarium_id)

    now = datetime.now(UTC)

    # Soft delete the aquarium
    aquarium.deleted_at = now

    # Cascade soft delete to fish
    fish_stmt = (
        select(Fish)
        .where(Fish.aquarium_id == aquarium_id)
        .where(Fish.deleted_at.is_(None))
    )
    fish_result = await db.execute(fish_stmt)
    fish_list = fish_result.scalars().all()

    for fish in fish_list:
        fish.deleted_at = now

    await db.flush()

    logger.info(
        "Soft deleted aquarium and associated fish",
        aquarium_id=aquarium_id,
        fish_count=len(fish_list),
        user_id=user_id,
    )
