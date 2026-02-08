"""Sync validation: entity ownership and access control."""

from uuid import UUID

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.schemas.sync import ChangeItem

from .exceptions import SyncAccessDeniedError, SyncValidationError

logger = structlog.get_logger(__name__)


async def _get_user_aquarium_ids(
    db: AsyncSession,
    user_id: UUID,
    include_deleted: bool = False,
) -> set[UUID]:
    """Get all aquarium IDs accessible to user.

    Args:
        db: Database session.
        user_id: User ID.
        include_deleted: If True, include soft-deleted aquariums (for delta sync).

    Returns:
        Set of aquarium IDs where user is owner or member.
    """
    stmt = (
        select(Aquarium.id)
        .distinct()
        .outerjoin(AquariumMember, Aquarium.id == AquariumMember.aquarium_id)
        .where(
            or_(
                Aquarium.owner_id == user_id,
                AquariumMember.user_id == user_id,
            ),
        )
    )

    if not include_deleted:
        stmt = stmt.where(Aquarium.deleted_at.is_(None))

    result = await db.execute(stmt)
    return set(result.scalars().all())


async def _validate_entity_ownership(
    db: AsyncSession,
    user_id: UUID,
    changes: list[ChangeItem],
) -> None:
    """Validate that all entities in changes belong to user's aquariums.

    For create operations, validates the aquarium_id in data.
    For update/delete operations, validates existing entity ownership.

    Args:
        db: Database session.
        user_id: User ID.
        changes: List of change items to validate.

    Raises:
        SyncAccessDeniedError: If user doesn't have access to an entity.
        SyncValidationError: If validation data is missing.
    """
    if not changes:
        return

    # Collect aquarium IDs being created in this batch so that fish/schedule
    # creates referencing a new aquarium from the same batch pass validation.
    pending_aquarium_ids = {
        change.entity_id
        for change in changes
        if change.entity_type == "aquarium" and change.operation == "create"
    }

    user_aquarium_ids = await _get_user_aquarium_ids(db, user_id)
    user_aquarium_ids |= pending_aquarium_ids

    for change in changes:
        if change.entity_type == "aquarium":
            # For aquariums, check if it's in user's accessible aquariums
            if change.operation == "create":
                # New aquarium will be owned by user, always allowed
                continue
            # For update/delete, aquarium must be in user's list
            if change.entity_id not in user_aquarium_ids:
                raise SyncAccessDeniedError("aquarium", change.entity_id)

        elif change.entity_type == "fish":
            if change.operation == "create":
                # For create, check aquarium_id in data
                aquarium_id = change.data.get("aquarium_id")
                if not aquarium_id:
                    raise SyncValidationError(f"Missing aquarium_id for fish create: {change.entity_id}")
                try:
                    aquarium_uuid = UUID(str(aquarium_id))
                except (ValueError, TypeError):
                    raise SyncValidationError(f"Invalid aquarium_id format for fish: {change.entity_id}") from None
                if aquarium_uuid not in user_aquarium_ids:
                    raise SyncAccessDeniedError("aquarium", aquarium_uuid)
            else:
                # For update/delete, check existing fish ownership
                stmt = select(Fish.aquarium_id).where(Fish.id == change.entity_id)
                result = await db.execute(stmt)
                aquarium_id = result.scalar_one_or_none()
                if aquarium_id is None:
                    # Entity doesn't exist - will be handled by apply_changes
                    continue
                if aquarium_id not in user_aquarium_ids:
                    raise SyncAccessDeniedError("fish", change.entity_id)

        elif change.entity_type == "feeding_log":
            if change.operation == "create":
                # For create, check aquarium_id in data
                aquarium_id = change.data.get("aquarium_id")
                if not aquarium_id:
                    raise SyncValidationError(f"Missing aquarium_id for event create: {change.entity_id}")
                try:
                    aquarium_uuid = UUID(str(aquarium_id))
                except (ValueError, TypeError):
                    raise SyncValidationError(f"Invalid aquarium_id format for event: {change.entity_id}") from None
                if aquarium_uuid not in user_aquarium_ids:
                    raise SyncAccessDeniedError("aquarium", aquarium_uuid)
            else:
                # For update/delete, check existing event ownership
                stmt = select(FeedingLog.aquarium_id).where(FeedingLog.id == change.entity_id)
                result = await db.execute(stmt)
                aquarium_id = result.scalar_one_or_none()
                if aquarium_id is None:
                    # Entity doesn't exist - will be handled by apply_changes
                    continue
                if aquarium_id not in user_aquarium_ids:
                    raise SyncAccessDeniedError("feeding_log", change.entity_id)

        elif change.entity_type == "schedule":
            if change.operation == "create":
                # For create, check aquarium_id in data
                aquarium_id = change.data.get("aquarium_id")
                if not aquarium_id:
                    raise SyncValidationError(f"Missing aquarium_id for schedule create: {change.entity_id}")
                try:
                    aquarium_uuid = UUID(str(aquarium_id))
                except (ValueError, TypeError):
                    raise SyncValidationError(f"Invalid aquarium_id format for schedule: {change.entity_id}") from None
                if aquarium_uuid not in user_aquarium_ids:
                    raise SyncAccessDeniedError("aquarium", aquarium_uuid)
            else:
                # For update/delete, check existing schedule ownership
                stmt = select(FeedingSchedule.aquarium_id).where(FeedingSchedule.id == change.entity_id)
                result = await db.execute(stmt)
                aquarium_id = result.scalar_one_or_none()
                if aquarium_id is None:
                    # Entity doesn't exist - will be handled by apply_changes
                    continue
                if aquarium_id not in user_aquarium_ids:
                    raise SyncAccessDeniedError("schedule", change.entity_id)

        elif change.entity_type in ("streak", "achievement", "progress", "user_profile"):
            # User-scoped entities - always allowed for the authenticated user
            # The apply_*_change functions will enforce that they can only
            # modify their own records by using user_id from the session
            continue
