"""Sync service with business logic for offline-first data synchronization."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingEvent, FeedingSchedule
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak, UserProgress
from app.schemas.sync import (
    ChangeItem,
    ConflictItem,
    DeletedEntities,
    EntityType,
    ServerState,
    SyncRequest,
    SyncResponse,
)
from app.services import feeding as feeding_service

logger = logging.getLogger(__name__)

# Time window for concurrent feeding detection (5 minutes)
CONCURRENT_FEEDING_WINDOW = timedelta(minutes=5)


class SyncError(Exception):
    """Base exception for sync errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class SyncValidationError(SyncError):
    """Raised when sync request validation fails."""

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class SyncAccessDeniedError(SyncError):
    """Raised when user doesn't have access to synced entities."""

    def __init__(self, entity_type: str, entity_id: UUID):
        super().__init__(f"Access denied to {entity_type} '{entity_id}'", status_code=403)


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

    user_aquarium_ids = await _get_user_aquarium_ids(db, user_id)

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

        elif change.entity_type == "event":
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
                stmt = select(FeedingEvent.aquarium_id).where(FeedingEvent.id == change.entity_id)
                result = await db.execute(stmt)
                aquarium_id = result.scalar_one_or_none()
                if aquarium_id is None:
                    # Entity doesn't exist - will be handled by apply_changes
                    continue
                if aquarium_id not in user_aquarium_ids:
                    raise SyncAccessDeniedError("event", change.entity_id)

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

        elif change.entity_type in ("streak", "achievement", "progress"):
            # User-scoped entities - always allowed for the authenticated user
            # The apply_*_change functions will enforce that they can only
            # modify their own records by using user_id from the session
            continue


def _generate_sync_token() -> str:
    """Generate a unique sync token.

    Uses combination of timestamp and UUID for uniqueness.

    Returns:
        Sync token string.
    """
    now = datetime.now(UTC)
    timestamp = now.strftime("%Y%m%d%H%M%S%f")
    unique_id = uuid4().hex[:8]
    return f"{timestamp}-{unique_id}"


def resolve_conflict(
    server_updated_at: datetime,
    client_updated_at: datetime,
) -> str:
    """Determine winner based on timestamp comparison (last-write-wins).

    Args:
        server_updated_at: Server entity's updated_at timestamp.
        client_updated_at: Client's updated_at timestamp.

    Returns:
        'client' if client timestamp is newer, 'server' otherwise.
        When timestamps are equal, server wins for determinism.
    """
    # Normalize both datetimes to UTC for comparison
    # Handle both timezone-aware and timezone-naive datetimes
    server_ts = server_updated_at
    client_ts = client_updated_at

    if server_ts.tzinfo is None:
        server_ts = server_ts.replace(tzinfo=UTC)
    if client_ts.tzinfo is None:
        client_ts = client_ts.replace(tzinfo=UTC)

    if client_ts > server_ts:
        return "client"
    return "server"


def _entity_to_dict(
    entity: Aquarium | Fish | FeedingEvent | FeedingSchedule | Streak | Achievement | UserProgress,
) -> dict[str, Any]:
    """Convert entity to dictionary for conflict reporting.

    Args:
        entity: Database entity.

    Returns:
        Dictionary representation of entity.
    """
    result: dict[str, Any] = {}

    if isinstance(entity, Aquarium):
        result = {
            "id": str(entity.id),
            "owner_id": str(entity.owner_id),
            "name": entity.name,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
            "deleted_at": entity.deleted_at.isoformat() if entity.deleted_at else None,
        }
    elif isinstance(entity, Fish):
        result = {
            "id": str(entity.id),
            "aquarium_id": str(entity.aquarium_id),
            "species_id": entity.species_id,
            "quantity": entity.quantity,
            "custom_name": entity.custom_name,
            "added_via": entity.added_via,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
            "deleted_at": entity.deleted_at.isoformat() if entity.deleted_at else None,
        }
    elif isinstance(entity, FeedingEvent):
        result = {
            "id": str(entity.id),
            "aquarium_id": str(entity.aquarium_id),
            "schedule_id": str(entity.schedule_id) if entity.schedule_id else None,
            "fish_id": str(entity.fish_id) if entity.fish_id else None,
            "species_id": entity.species_id,
            "scheduled_at": (entity.scheduled_at.isoformat() if entity.scheduled_at else None),
            "status": entity.status,
            "completed_at": (entity.completed_at.isoformat() if entity.completed_at else None),
            "completed_by": str(entity.completed_by) if entity.completed_by else None,
            "concurrent_with": (str(entity.concurrent_with) if entity.concurrent_with else None),
            "client_created_at": (entity.client_created_at.isoformat() if entity.client_created_at else None),
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
            "deleted_at": entity.deleted_at.isoformat() if entity.deleted_at else None,
        }
    elif isinstance(entity, FeedingSchedule):
        result = {
            "id": str(entity.id),
            "aquarium_id": str(entity.aquarium_id),
            "times_per_day": entity.times_per_day,
            "scheduled_times": entity.scheduled_times,
            "food_type": entity.food_type,
            "portion_hint": entity.portion_hint,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
        }
    elif isinstance(entity, Streak):
        result = {
            "id": str(entity.user_id),  # user_id is the primary key for streaks
            "user_id": str(entity.user_id),
            "current_streak": entity.current_streak,
            "best_streak": entity.best_streak,
            "freeze_available": entity.freeze_available,
            "freeze_used_this_period": entity.freeze_used_this_period,
            "period_start": entity.period_start.isoformat() if entity.period_start else None,
            "last_feed_date": entity.last_feed_date.isoformat() if entity.last_feed_date else None,
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
        }
    elif isinstance(entity, Achievement):
        result = {
            "id": str(entity.id),
            "user_id": str(entity.user_id),
            "achievement_type": entity.achievement_type,
            "unlocked_at": entity.unlocked_at.isoformat() if entity.unlocked_at else None,
            "shared_at": entity.shared_at.isoformat() if entity.shared_at else None,
        }
    elif isinstance(entity, UserProgress):
        result = {
            "id": str(entity.user_id),  # user_id is the primary key for progress
            "user_id": str(entity.user_id),
            "total_xp": entity.total_xp,
            "level": entity.level,
            "last_xp_awarded_at": (entity.last_xp_awarded_at.isoformat() if entity.last_xp_awarded_at else None),
            "last_level_up_at": (entity.last_level_up_at.isoformat() if entity.last_level_up_at else None),
            "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
        }

    return result


async def _detect_concurrent_feeding(
    db: AsyncSession,
    aquarium_id: UUID,
    schedule_id: UUID | None,
    completed_at: datetime,
    completed_by: UUID,
    exclude_event_id: UUID | None = None,
) -> FeedingEvent | None:
    """Detect if there's a concurrent feeding event within the time window.

    Concurrent feeding occurs when two different users complete feeding events
    for the same aquarium and schedule within a short time window.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID to check.
        schedule_id: Schedule ID to match (if None, matches events without schedule).
        completed_at: Completion timestamp of the new event.
        completed_by: User ID who completed the new event.
        exclude_event_id: Event ID to exclude from search (the event being created/updated).

    Returns:
        Existing concurrent FeedingEvent if found, None otherwise.
    """
    if schedule_id is None:
        # For ad-hoc feedings (no schedule), skip concurrent detection
        return None

    # Calculate time window boundaries
    window_start = completed_at - CONCURRENT_FEEDING_WINDOW
    window_end = completed_at + CONCURRENT_FEEDING_WINDOW

    # Build query for concurrent events
    stmt = select(FeedingEvent).where(
        and_(
            FeedingEvent.aquarium_id == aquarium_id,
            FeedingEvent.schedule_id == schedule_id,
            FeedingEvent.status == "completed",
            FeedingEvent.completed_at.is_not(None),
            FeedingEvent.completed_at >= window_start,
            FeedingEvent.completed_at <= window_end,
            FeedingEvent.completed_by != completed_by,  # Different user
        )
    )

    # Exclude the current event if updating
    if exclude_event_id:
        stmt = stmt.where(FeedingEvent.id != exclude_event_id)

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _group_changes_by_entity_type(
    changes: list[ChangeItem],
) -> dict[EntityType, list[ChangeItem]]:
    """Group changes by entity type for batch processing.

    Args:
        changes: List of change items.

    Returns:
        Dictionary mapping entity types to their changes.
    """
    grouped: dict[EntityType, list[ChangeItem]] = {
        "aquarium": [],
        "fish": [],
        "event": [],
        "schedule": [],
        "streak": [],
        "achievement": [],
        "progress": [],
    }
    for change in changes:
        grouped[change.entity_type].append(change)
    return grouped


async def _apply_aquarium_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single aquarium change.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if conflict detected, None otherwise.
    """
    stmt = select(Aquarium).where(Aquarium.id == change.entity_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if change.operation == "create":
        if existing is not None:
            # Entity exists, treat as update with conflict check
            winner = resolve_conflict(existing.updated_at, change.client_updated_at)
            if winner == "server":
                logger.debug(f"CREATE conflict for aquarium {change.entity_id}: server wins")
                return ConflictItem(
                    entity_type="aquarium",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution="server_wins",
                )
            # Client wins - update existing
            if "name" in change.data:
                existing.name = change.data["name"]
            logger.debug(f"CREATE->UPDATE aquarium {change.entity_id}: client wins")
        else:
            # Create new aquarium
            aquarium = Aquarium(
                id=change.entity_id,
                owner_id=user_id,
                name=change.data.get("name", "Unnamed Aquarium"),
            )
            db.add(aquarium)
            # Also add owner as member
            member = AquariumMember(
                aquarium_id=change.entity_id,
                user_id=user_id,
                role="owner",
            )
            db.add(member)
            logger.debug(f"Created aquarium {change.entity_id}")

    elif change.operation == "update":
        if existing is None:
            logger.debug(f"UPDATE skipped for non-existent aquarium {change.entity_id}")
            return None

        if existing.deleted_at is not None:
            logger.debug(f"UPDATE skipped for deleted aquarium {change.entity_id}")
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"UPDATE conflict for aquarium {change.entity_id}: server wins")
            return ConflictItem(
                entity_type="aquarium",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )

        # Client wins - apply update
        if "name" in change.data:
            existing.name = change.data["name"]
        logger.debug(f"Updated aquarium {change.entity_id}")

    elif change.operation == "delete":
        if existing is None:
            logger.debug(f"DELETE skipped for non-existent aquarium {change.entity_id}")
            return None

        if existing.deleted_at is not None:
            logger.debug(f"DELETE skipped for already deleted aquarium {change.entity_id}")
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"DELETE conflict for aquarium {change.entity_id}: server wins")
            return ConflictItem(
                entity_type="aquarium",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )

        # Client wins - soft delete
        existing.deleted_at = datetime.now(UTC)
        logger.debug(f"Soft deleted aquarium {change.entity_id}")

    return None


async def _apply_fish_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single fish change.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if conflict detected, None otherwise.
    """
    stmt = select(Fish).where(Fish.id == change.entity_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if change.operation == "create":
        if existing is not None:
            # Entity exists, treat as update with conflict check
            winner = resolve_conflict(existing.updated_at, change.client_updated_at)
            if winner == "server":
                logger.debug(f"CREATE conflict for fish {change.entity_id}: server wins")
                return ConflictItem(
                    entity_type="fish",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution="server_wins",
                )
            # Client wins - update existing
            if "quantity" in change.data:
                existing.quantity = change.data["quantity"]
            if "custom_name" in change.data:
                existing.custom_name = change.data["custom_name"]
            if "species_id" in change.data:
                existing.species_id = change.data["species_id"]
            logger.debug(f"CREATE->UPDATE fish {change.entity_id}: client wins")
        else:
            # Create new fish
            aquarium_id = change.data.get("aquarium_id")
            if aquarium_id:
                aquarium_id = UUID(str(aquarium_id))

            fish = Fish(
                id=change.entity_id,
                aquarium_id=aquarium_id,
                species_id=change.data.get("species_id", "unknown"),
                quantity=change.data.get("quantity", 1),
                custom_name=change.data.get("custom_name"),
                added_via=change.data.get("added_via", "sync"),
            )
            db.add(fish)
            logger.debug(f"Created fish {change.entity_id}")

    elif change.operation == "update":
        if existing is None:
            logger.debug(f"UPDATE skipped for non-existent fish {change.entity_id}")
            return None

        if existing.deleted_at is not None:
            logger.debug(f"UPDATE skipped for deleted fish {change.entity_id}")
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"UPDATE conflict for fish {change.entity_id}: server wins")
            return ConflictItem(
                entity_type="fish",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )

        # Client wins - apply update
        if "quantity" in change.data:
            existing.quantity = change.data["quantity"]
        if "custom_name" in change.data:
            existing.custom_name = change.data["custom_name"]
        if "species_id" in change.data:
            existing.species_id = change.data["species_id"]
        logger.debug(f"Updated fish {change.entity_id}")

    elif change.operation == "delete":
        if existing is None:
            logger.debug(f"DELETE skipped for non-existent fish {change.entity_id}")
            return None

        if existing.deleted_at is not None:
            logger.debug(f"DELETE skipped for already deleted fish {change.entity_id}")
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"DELETE conflict for fish {change.entity_id}: server wins")
            return ConflictItem(
                entity_type="fish",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )

        # Client wins - soft delete
        now = datetime.now(UTC)
        existing.deleted_at = now
        logger.debug(f"Soft deleted fish {change.entity_id}")

        # Cascade soft delete: also soft delete feeding events for this fish
        cascade_stmt = (
            select(FeedingEvent)
            .where(FeedingEvent.fish_id == change.entity_id)
            .where(FeedingEvent.deleted_at.is_(None))
        )
        events_result = await db.execute(cascade_stmt)
        events_to_delete = events_result.scalars().all()
        for event in events_to_delete:
            event.deleted_at = now
        if events_to_delete:
            logger.debug(f"Cascade soft deleted {len(events_to_delete)} feeding events for fish {change.entity_id}")

    return None


async def _apply_event_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single feeding event change.

    Includes special handling for concurrent feeding detection: when two users
    complete the same scheduled feeding within a 5-minute window, returns a
    concurrent_feeding conflict instead of applying last-write-wins.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if conflict detected, None otherwise.
    """
    stmt = select(FeedingEvent).where(FeedingEvent.id == change.entity_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if change.operation == "create":
        if existing is not None:
            # Entity exists, treat as update with conflict check
            winner = resolve_conflict(existing.updated_at, change.client_updated_at)
            if winner == "server":
                logger.debug(f"CREATE conflict for event {change.entity_id}: server wins")
                return ConflictItem(
                    entity_type="event",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution="server_wins",
                )
            # Client wins - update existing
            if "status" in change.data:
                existing.status = change.data["status"]
            if "completed_at" in change.data:
                completed_at = change.data["completed_at"]
                if isinstance(completed_at, str):
                    existing.completed_at = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                else:
                    existing.completed_at = completed_at
            if "completed_by" in change.data:
                existing.completed_by = UUID(str(change.data["completed_by"])) if change.data["completed_by"] else None
            logger.debug(f"CREATE->UPDATE event {change.entity_id}: client wins")
        else:
            # Create new event
            aquarium_id = change.data.get("aquarium_id")
            if aquarium_id:
                aquarium_id = UUID(str(aquarium_id))

            scheduled_at_str = change.data.get("scheduled_at")
            if isinstance(scheduled_at_str, str):
                scheduled_at = datetime.fromisoformat(scheduled_at_str.replace("Z", "+00:00"))
            else:
                scheduled_at = scheduled_at_str or datetime.now(UTC)

            # Parse schedule_id for concurrent feeding detection
            schedule_id: UUID | None = None
            if "schedule_id" in change.data and change.data["schedule_id"]:
                schedule_id = UUID(str(change.data["schedule_id"]))

            # Parse fish_id (UUID) and species_id (string)
            fish_id: UUID | None = None
            species_id: str | None = None
            if "fish_id" in change.data and change.data["fish_id"]:
                fish_id_value = str(change.data["fish_id"])
                try:
                    fish_id = UUID(fish_id_value)
                except (ValueError, TypeError):
                    # fish_id is not a valid UUID - treat as species_id
                    species_id = fish_id_value
            # Also check for explicit species_id field
            if "species_id" in change.data and change.data["species_id"]:
                species_id = str(change.data["species_id"])

            # Parse completed_at and completed_by for concurrent feeding detection
            event_completed_at: datetime | None = None
            event_completed_by: UUID | None = None
            status = change.data.get("status", "pending")

            if "completed_at" in change.data and change.data["completed_at"]:
                ca = change.data["completed_at"]
                if isinstance(ca, str):
                    event_completed_at = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                else:
                    event_completed_at = ca

            if "completed_by" in change.data and change.data["completed_by"]:
                event_completed_by = UUID(str(change.data["completed_by"]))

            # Check for concurrent feeding before creating
            if (
                status == "completed"
                and aquarium_id is not None
                and schedule_id is not None
                and event_completed_at is not None
                and event_completed_by is not None
            ):
                concurrent_event = await _detect_concurrent_feeding(
                    db=db,
                    aquarium_id=aquarium_id,
                    schedule_id=schedule_id,
                    completed_at=event_completed_at,
                    completed_by=event_completed_by,
                    exclude_event_id=None,
                )
                if concurrent_event is not None:
                    logger.info(
                        f"Concurrent feeding detected for event {change.entity_id}: "
                        f"conflicts with {concurrent_event.id}"
                    )
                    return ConflictItem(
                        entity_type="event",
                        entity_id=change.entity_id,
                        client_data=change.data,
                        server_data=_entity_to_dict(concurrent_event),
                        client_updated_at=change.client_updated_at,
                        server_updated_at=concurrent_event.updated_at,
                        resolution="concurrent_feeding",
                    )

            event = FeedingEvent(
                id=change.entity_id,
                aquarium_id=aquarium_id,
                scheduled_at=scheduled_at,
                status=status,
                client_created_at=change.client_updated_at,
                schedule_id=schedule_id,
                fish_id=fish_id,
                species_id=species_id,
                completed_at=event_completed_at,
                completed_by=event_completed_by,
            )

            db.add(event)
            logger.debug(f"Created event {change.entity_id}")

    elif change.operation == "update":
        if existing is None:
            logger.debug(f"UPDATE skipped for non-existent event {change.entity_id}")
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"UPDATE conflict for event {change.entity_id}: server wins")
            return ConflictItem(
                entity_type="event",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )

        # Parse update values (use existing values as fallback)
        new_status = change.data.get("status", existing.status)

        new_completed_at = existing.completed_at
        if "completed_at" in change.data and change.data["completed_at"]:
            ca = change.data["completed_at"]
            if isinstance(ca, str):
                new_completed_at = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            else:
                new_completed_at = ca

        new_completed_by = existing.completed_by
        if "completed_by" in change.data:
            new_completed_by = UUID(str(change.data["completed_by"])) if change.data["completed_by"] else None

        # Check for concurrent feeding if updating to completed status
        if (
            new_status == "completed"
            and existing.schedule_id is not None
            and new_completed_at is not None
            and new_completed_by is not None
        ):
            concurrent_event = await _detect_concurrent_feeding(
                db=db,
                aquarium_id=existing.aquarium_id,
                schedule_id=existing.schedule_id,
                completed_at=new_completed_at,
                completed_by=new_completed_by,
                exclude_event_id=existing.id,
            )
            if concurrent_event is not None:
                logger.info(
                    f"Concurrent feeding detected for event {change.entity_id}: conflicts with {concurrent_event.id}"
                )
                return ConflictItem(
                    entity_type="event",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(concurrent_event),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=concurrent_event.updated_at,
                    resolution="concurrent_feeding",
                )

        # Client wins - apply update
        existing.status = new_status
        existing.completed_at = new_completed_at
        existing.completed_by = new_completed_by

        if "scheduled_at" in change.data:
            scheduled_at = change.data["scheduled_at"]
            if isinstance(scheduled_at, str):
                existing.scheduled_at = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            else:
                existing.scheduled_at = scheduled_at
        logger.debug(f"Updated event {change.entity_id}")

    elif change.operation == "delete":
        if existing is None:
            logger.debug(f"DELETE skipped for non-existent event {change.entity_id}")
            return None

        if existing.deleted_at is not None:
            logger.debug(f"DELETE skipped for already deleted event {change.entity_id}")
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"DELETE conflict for event {change.entity_id}: server wins")
            return ConflictItem(
                entity_type="event",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )

        # Client wins - soft delete
        existing.deleted_at = datetime.now(UTC)
        logger.debug(f"Soft deleted event {change.entity_id}")

    return None


async def _apply_schedule_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single feeding schedule change.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if conflict detected, None otherwise.
    """
    stmt = select(FeedingSchedule).where(FeedingSchedule.id == change.entity_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if change.operation == "create":
        if existing is not None:
            # Entity exists, treat as update with conflict check
            winner = resolve_conflict(existing.updated_at, change.client_updated_at)
            if winner == "server":
                logger.debug(f"CREATE conflict for schedule {change.entity_id}: server wins")
                return ConflictItem(
                    entity_type="schedule",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution="server_wins",
                )
            # Client wins - update existing
            if "times_per_day" in change.data:
                existing.times_per_day = change.data["times_per_day"]
            if "scheduled_times" in change.data:
                existing.scheduled_times = change.data["scheduled_times"]
            if "food_type" in change.data:
                existing.food_type = change.data["food_type"]
            if "portion_hint" in change.data:
                existing.portion_hint = change.data["portion_hint"]
            logger.debug(f"CREATE->UPDATE schedule {change.entity_id}: client wins")
        else:
            # Create new schedule
            aquarium_id = change.data.get("aquarium_id")
            if aquarium_id:
                aquarium_id = UUID(str(aquarium_id))

            schedule = FeedingSchedule(
                id=change.entity_id,
                aquarium_id=aquarium_id,
                times_per_day=change.data.get("times_per_day", 2),
                scheduled_times=change.data.get("scheduled_times", []),
                food_type=change.data.get("food_type", "flakes"),
                portion_hint=change.data.get("portion_hint"),
            )
            db.add(schedule)
            logger.debug(f"Created schedule {change.entity_id}")

    elif change.operation == "update":
        if existing is None:
            logger.debug(f"UPDATE skipped for non-existent schedule {change.entity_id}")
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"UPDATE conflict for schedule {change.entity_id}: server wins")
            return ConflictItem(
                entity_type="schedule",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )

        # Client wins - apply update
        if "times_per_day" in change.data:
            existing.times_per_day = change.data["times_per_day"]
        if "scheduled_times" in change.data:
            existing.scheduled_times = change.data["scheduled_times"]
        if "food_type" in change.data:
            existing.food_type = change.data["food_type"]
        if "portion_hint" in change.data:
            existing.portion_hint = change.data["portion_hint"]
        logger.debug(f"Updated schedule {change.entity_id}")

    elif change.operation == "delete":
        if existing is None:
            logger.debug(f"DELETE skipped for non-existent schedule {change.entity_id}")
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"DELETE conflict for schedule {change.entity_id}: server wins")
            return ConflictItem(
                entity_type="schedule",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )

        # Client wins - hard delete
        await db.delete(existing)
        logger.debug(f"Hard deleted schedule {change.entity_id}")

    return None


async def _apply_streak_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single streak change.

    Streaks are user-scoped and use user_id as primary key.
    Only update operations are allowed - streaks are created server-side.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if conflict detected, None otherwise.
    """
    # Streak uses user_id as primary key
    stmt = select(Streak).where(Streak.user_id == user_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if change.operation in ("create", "update"):
        if existing is None:
            # Create new streak for user
            from datetime import date as date_type

            last_feed_date = None
            if "last_feed_date" in change.data and change.data["last_feed_date"]:
                lfd = change.data["last_feed_date"]
                if isinstance(lfd, str):
                    last_feed_date = date_type.fromisoformat(lfd[:10])
                else:
                    last_feed_date = lfd

            streak = Streak(
                user_id=user_id,
                current_streak=change.data.get("current_streak", 0),
                best_streak=change.data.get("best_streak", 0),
                freeze_available=change.data.get("freeze_available", 2),
                freeze_used_this_period=change.data.get("freeze_used_this_period", 0),
                last_feed_date=last_feed_date,
            )
            db.add(streak)
            logger.debug(f"Created streak for user {user_id}")
        else:
            # Check for conflict
            winner = resolve_conflict(existing.updated_at, change.client_updated_at)
            if winner == "server":
                logger.debug(f"UPDATE conflict for streak {user_id}: server wins")
                return ConflictItem(
                    entity_type="streak",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution="server_wins",
                )

            # Client wins - apply update
            if "current_streak" in change.data:
                existing.current_streak = change.data["current_streak"]
            if "best_streak" in change.data:
                existing.best_streak = change.data["best_streak"]
            if "freeze_available" in change.data:
                existing.freeze_available = change.data["freeze_available"]
            if "freeze_used_this_period" in change.data:
                existing.freeze_used_this_period = change.data["freeze_used_this_period"]
            if "last_feed_date" in change.data:
                from datetime import date as date_type

                lfd = change.data["last_feed_date"]
                if lfd:
                    if isinstance(lfd, str):
                        existing.last_feed_date = date_type.fromisoformat(lfd[:10])
                    else:
                        existing.last_feed_date = lfd
                else:
                    existing.last_feed_date = None
            logger.debug(f"Updated streak for user {user_id}")

    # Delete operation not supported for streaks
    elif change.operation == "delete":
        logger.debug(f"DELETE not supported for streaks, ignoring {change.entity_id}")

    return None


async def _apply_achievement_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single achievement change.

    Achievements are typically created server-side when unlocked.
    Client can report achievement unlocks for offline scenarios.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if conflict detected, None otherwise.
    """
    stmt = select(Achievement).where(Achievement.id == change.entity_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if change.operation == "create":
        if existing is not None:
            # Achievement already exists - no conflict, just skip
            logger.debug(f"Achievement {change.entity_id} already exists, skipping")
            return None

        # Create new achievement
        unlocked_at = None
        if "unlocked_at" in change.data and change.data["unlocked_at"]:
            ua = change.data["unlocked_at"]
            if isinstance(ua, str):
                unlocked_at = datetime.fromisoformat(ua.replace("Z", "+00:00"))
            else:
                unlocked_at = ua

        achievement = Achievement(
            id=change.entity_id,
            user_id=user_id,
            achievement_type=change.data.get("achievement_type", "unknown"),
            unlocked_at=unlocked_at or datetime.now(UTC),
        )
        db.add(achievement)
        logger.debug(f"Created achievement {change.entity_id}")

    elif change.operation == "update":
        if existing is None:
            logger.debug(f"UPDATE skipped for non-existent achievement {change.entity_id}")
            return None

        # Achievements are mostly immutable, only shared_at can be updated
        if "shared_at" in change.data:
            sa = change.data["shared_at"]
            if sa:
                if isinstance(sa, str):
                    existing.shared_at = datetime.fromisoformat(sa.replace("Z", "+00:00"))
                else:
                    existing.shared_at = sa
            else:
                existing.shared_at = None
        logger.debug(f"Updated achievement {change.entity_id}")

    # Delete operation not supported for achievements
    elif change.operation == "delete":
        logger.debug(f"DELETE not supported for achievements, ignoring {change.entity_id}")

    return None


async def _apply_progress_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single user progress change.

    Progress is user-scoped and uses user_id as primary key.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if conflict detected, None otherwise.
    """
    # Progress uses user_id as primary key
    stmt = select(UserProgress).where(UserProgress.user_id == user_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if change.operation in ("create", "update"):
        if existing is None:
            # Create new progress for user
            progress = UserProgress(
                user_id=user_id,
                total_xp=change.data.get("total_xp", 0),
                level=change.data.get("level", 1),
            )
            db.add(progress)
            logger.debug(f"Created progress for user {user_id}")
        else:
            # Check for conflict
            winner = resolve_conflict(existing.updated_at, change.client_updated_at)
            if winner == "server":
                logger.debug(f"UPDATE conflict for progress {user_id}: server wins")
                return ConflictItem(
                    entity_type="progress",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution="server_wins",
                )

            # Client wins - apply update (XP can only go up)
            if "total_xp" in change.data:
                new_xp = change.data["total_xp"]
                if new_xp > existing.total_xp:
                    existing.total_xp = new_xp
                    existing.last_xp_awarded_at = datetime.now(UTC)
            if "level" in change.data:
                new_level = change.data["level"]
                if new_level > existing.level:
                    existing.level = new_level
                    existing.last_level_up_at = datetime.now(UTC)
            logger.debug(f"Updated progress for user {user_id}")

    # Delete operation not supported for progress
    elif change.operation == "delete":
        logger.debug(f"DELETE not supported for progress, ignoring {change.entity_id}")

    return None


async def apply_changes(
    db: AsyncSession,
    user_id: UUID,
    changes: list[ChangeItem],
) -> list[ConflictItem]:
    """Apply client changes to server with last-write-wins conflict resolution.

    Processes changes grouped by entity type for batch optimization.
    Uses last-write-wins strategy: newer timestamp wins, server wins on tie.

    Args:
        db: Database session.
        user_id: User ID applying the changes.
        changes: List of changes from client.

    Returns:
        List of conflicts detected during sync.
    """
    if not changes:
        return []

    conflicts: list[ConflictItem] = []

    # Group changes by entity type for batch processing
    grouped = _group_changes_by_entity_type(changes)

    logger.debug(
        f"Applying changes for user {user_id}: "
        f"{len(grouped['aquarium'])} aquariums, "
        f"{len(grouped['fish'])} fish, "
        f"{len(grouped['event'])} events, "
        f"{len(grouped['schedule'])} schedules, "
        f"{len(grouped['streak'])} streaks, "
        f"{len(grouped['achievement'])} achievements, "
        f"{len(grouped['progress'])} progress"
    )

    # Process aquarium changes
    for change in grouped["aquarium"]:
        conflict = await _apply_aquarium_change(db, user_id, change)
        if conflict:
            conflicts.append(conflict)

    # Process fish changes and collect affected aquarium IDs
    affected_aquarium_ids: set[UUID] = set()
    for change in grouped["fish"]:
        conflict = await _apply_fish_change(db, user_id, change)
        if conflict:
            conflicts.append(conflict)
        else:
            # Track aquarium for schedule generation
            if change.operation == "create":
                aquarium_id = change.data.get("aquarium_id")
                if aquarium_id:
                    affected_aquarium_ids.add(UUID(str(aquarium_id)))
            elif change.operation in ("update", "delete"):
                # Get aquarium_id from existing fish
                stmt = select(Fish.aquarium_id).where(Fish.id == change.entity_id)
                result = await db.execute(stmt)
                aquarium_id = result.scalar_one_or_none()
                if aquarium_id:
                    affected_aquarium_ids.add(aquarium_id)

    # Process event changes
    for change in grouped["event"]:
        conflict = await _apply_event_change(db, user_id, change)
        if conflict:
            conflicts.append(conflict)

    # Process schedule changes
    for change in grouped["schedule"]:
        conflict = await _apply_schedule_change(db, user_id, change)
        if conflict:
            conflicts.append(conflict)

    # Process streak changes (user-scoped)
    for change in grouped["streak"]:
        conflict = await _apply_streak_change(db, user_id, change)
        if conflict:
            conflicts.append(conflict)

    # Process achievement changes (user-scoped)
    for change in grouped["achievement"]:
        conflict = await _apply_achievement_change(db, user_id, change)
        if conflict:
            conflicts.append(conflict)

    # Process progress changes (user-scoped)
    for change in grouped["progress"]:
        conflict = await _apply_progress_change(db, user_id, change)
        if conflict:
            conflicts.append(conflict)

    # Flush to ensure all changes are applied
    await db.flush()

    # Auto-generate feeding schedules for aquariums with fish changes
    if affected_aquarium_ids:
        logger.debug(f"Generating feeding schedules for {len(affected_aquarium_ids)} aquariums")
        for aquarium_id in affected_aquarium_ids:
            try:
                await feeding_service.generate_schedule(db, aquarium_id, user_id)
                logger.debug(f"Generated schedule for aquarium {aquarium_id}")
            except Exception as e:
                # Log but don't fail sync if schedule generation fails
                logger.warning(f"Failed to generate schedule for aquarium {aquarium_id}: {e}")

    logger.debug(f"Applied changes with {len(conflicts)} conflicts")
    return conflicts


async def _ensure_schedules_for_user(
    db: AsyncSession,
    user_id: UUID,
) -> None:
    """Ensure feeding schedules exist for all user's aquariums with fish.

    Checks each aquarium owned by user. If aquarium has fish but no schedule,
    generates a schedule based on fish species feeding requirements.

    Args:
        db: Database session.
        user_id: User ID.
    """
    # Get all user's aquarium IDs
    user_aquarium_ids = await _get_user_aquarium_ids(db, user_id)
    if not user_aquarium_ids:
        return

    # Get aquariums that have fish but no schedule
    aquariums_with_fish_stmt = (
        select(Fish.aquarium_id)
        .where(Fish.aquarium_id.in_(user_aquarium_ids))
        .where(Fish.deleted_at.is_(None))
        .distinct()
    )
    result = await db.execute(aquariums_with_fish_stmt)
    aquariums_with_fish = set(result.scalars().all())

    aquariums_with_schedule_stmt = select(FeedingSchedule.aquarium_id).where(
        FeedingSchedule.aquarium_id.in_(user_aquarium_ids)
    )
    result = await db.execute(aquariums_with_schedule_stmt)
    aquariums_with_schedule = set(result.scalars().all())

    # Find aquariums that need schedules
    aquariums_needing_schedule = aquariums_with_fish - aquariums_with_schedule

    if not aquariums_needing_schedule:
        return

    logger.info(f"Creating schedules for {len(aquariums_needing_schedule)} aquariums that have fish but no schedule")

    for aquarium_id in aquariums_needing_schedule:
        try:
            await feeding_service.generate_schedule(db, aquarium_id, user_id)
            logger.debug(f"Created schedule for aquarium {aquarium_id}")
        except Exception as e:
            logger.warning(f"Failed to create schedule for aquarium {aquarium_id}: {e}")


async def get_server_state(
    db: AsyncSession,
    user_id: UUID,
    since: datetime | None,
) -> ServerState:
    """Get server state for user, optionally filtered by last sync time.

    Implements delta sync: returns only entities changed after 'since' timestamp.
    For initial sync (since=None), returns all active entities.

    Args:
        db: Database session.
        user_id: User ID.
        since: If provided, return only changes after this timestamp (delta sync).

    Returns:
        ServerState with aquariums, fish, events, and deleted entity IDs.
    """
    logger.debug(f"get_server_state for user {user_id}, since={since}")

    # For delta sync, include deleted aquariums to track deletions
    # For initial sync, only active aquariums
    include_deleted = since is not None
    user_aquarium_ids = await _get_user_aquarium_ids(db, user_id, include_deleted=include_deleted)

    # Build queries based on delta sync or initial sync
    aquariums_data: list[dict[str, Any]] = []
    fish_data: list[dict[str, Any]] = []
    events_data: list[dict[str, Any]] = []
    schedules_data: list[dict[str, Any]] = []
    streaks_data: list[dict[str, Any]] = []
    achievements_data: list[dict[str, Any]] = []
    progress_data: list[dict[str, Any]] = []
    deleted = DeletedEntities()

    # Query user-scoped entities (streaks, achievements, progress)
    # These don't depend on aquariums
    # Query streak
    streak_stmt = select(Streak).where(Streak.user_id == user_id)
    if since is not None:
        streak_stmt = streak_stmt.where(Streak.updated_at >= since)
    streak_result = await db.execute(streak_stmt)
    for streak in streak_result.scalars().all():
        streaks_data.append(_entity_to_dict(streak))

    # Query achievements
    achievement_stmt = select(Achievement).where(Achievement.user_id == user_id)
    if since is not None:
        achievement_stmt = achievement_stmt.where(Achievement.unlocked_at >= since)
    achievement_result = await db.execute(achievement_stmt)
    for achievement in achievement_result.scalars().all():
        achievements_data.append(_entity_to_dict(achievement))

    # Query progress
    progress_stmt = select(UserProgress).where(UserProgress.user_id == user_id)
    if since is not None:
        progress_stmt = progress_stmt.where(UserProgress.updated_at >= since)
    progress_result = await db.execute(progress_stmt)
    for progress in progress_result.scalars().all():
        progress_data.append(_entity_to_dict(progress))

    if not user_aquarium_ids:
        logger.debug(f"No aquariums found for user {user_id}")
        return ServerState(
            aquariums=[],
            fish=[],
            events=[],
            schedules=[],
            streaks=streaks_data,
            achievements=achievements_data,
            progress=progress_data,
            deleted=DeletedEntities(),
        )

    # Query aquariums
    aquarium_stmt = select(Aquarium).where(Aquarium.id.in_(user_aquarium_ids))

    if since is not None:
        # Delta sync: get updated and deleted aquariums
        # Active aquariums updated after 'since' (use >= for timing edge cases)
        active_aquarium_stmt = aquarium_stmt.where(
            Aquarium.deleted_at.is_(None),
            Aquarium.updated_at >= since,
        )
        result = await db.execute(active_aquarium_stmt)
        for aquarium in result.scalars().all():
            aquariums_data.append(_entity_to_dict(aquarium))

        # Deleted aquariums after 'since'
        deleted_aquarium_stmt = aquarium_stmt.where(
            Aquarium.deleted_at.is_not(None),
            Aquarium.deleted_at >= since,
        )
        result = await db.execute(deleted_aquarium_stmt)
        deleted.aquariums = [aq.id for aq in result.scalars().all()]
    else:
        # Initial sync: get all active aquariums
        active_aquarium_stmt = aquarium_stmt.where(Aquarium.deleted_at.is_(None))
        result = await db.execute(active_aquarium_stmt)
        for aquarium in result.scalars().all():
            aquariums_data.append(_entity_to_dict(aquarium))

    # Query fish
    fish_stmt = select(Fish).where(Fish.aquarium_id.in_(user_aquarium_ids))

    if since is not None:
        # Delta sync: get updated and deleted fish
        active_fish_stmt = fish_stmt.where(
            Fish.deleted_at.is_(None),
            Fish.updated_at >= since,
        )
        result = await db.execute(active_fish_stmt)
        for fish in result.scalars().all():
            fish_data.append(_entity_to_dict(fish))

        # Deleted fish after 'since'
        deleted_fish_stmt = fish_stmt.where(
            Fish.deleted_at.is_not(None),
            Fish.deleted_at >= since,
        )
        result = await db.execute(deleted_fish_stmt)
        deleted.fish = [f.id for f in result.scalars().all()]
    else:
        # Initial sync: get all active fish
        active_fish_stmt = fish_stmt.where(Fish.deleted_at.is_(None))
        result = await db.execute(active_fish_stmt)
        for fish in result.scalars().all():
            fish_data.append(_entity_to_dict(fish))

    # Query feeding events
    event_stmt = select(FeedingEvent).where(FeedingEvent.aquarium_id.in_(user_aquarium_ids))

    if since is not None:
        # Delta sync: get updated and deleted events
        # Active events updated after 'since' (use >= for timing edge cases)
        active_event_stmt = event_stmt.where(
            FeedingEvent.deleted_at.is_(None),
            FeedingEvent.updated_at >= since,
        )
        result = await db.execute(active_event_stmt)
        for event in result.scalars().all():
            events_data.append(_entity_to_dict(event))

        # Deleted events after 'since'
        deleted_event_stmt = event_stmt.where(
            FeedingEvent.deleted_at.is_not(None),
            FeedingEvent.deleted_at >= since,
        )
        result = await db.execute(deleted_event_stmt)
        deleted.events = [e.id for e in result.scalars().all()]
    else:
        # Initial sync: get all active events only
        active_event_stmt = event_stmt.where(FeedingEvent.deleted_at.is_(None))
        result = await db.execute(active_event_stmt)
        for event in result.scalars().all():
            events_data.append(_entity_to_dict(event))

    # Query feeding schedules
    schedule_stmt = select(FeedingSchedule).where(FeedingSchedule.aquarium_id.in_(user_aquarium_ids))

    if since is not None:
        schedule_stmt = schedule_stmt.where(FeedingSchedule.updated_at >= since)

    result = await db.execute(schedule_stmt)
    for schedule in result.scalars().all():
        schedules_data.append(_entity_to_dict(schedule))

    logger.debug(
        f"get_server_state returning: "
        f"{len(aquariums_data)} aquariums, {len(fish_data)} fish, "
        f"{len(events_data)} events, {len(schedules_data)} schedules, "
        f"{len(streaks_data)} streaks, {len(achievements_data)} achievements, "
        f"{len(progress_data)} progress, "
        f"{len(deleted.aquariums)} deleted aquariums, "
        f"{len(deleted.fish)} deleted fish, "
        f"{len(deleted.events)} deleted events"
    )

    return ServerState(
        aquariums=aquariums_data,
        fish=fish_data,
        events=events_data,
        schedules=schedules_data,
        streaks=streaks_data,
        achievements=achievements_data,
        progress=progress_data,
        deleted=deleted,
    )


def _apply_pagination(
    server_state: ServerState,
    page_size: int,
    cursor: str | None,
) -> tuple[ServerState, bool, str | None]:
    """Apply pagination to server state.

    Paginates across all entity types in order: aquariums, fish, events, schedules.
    User-scoped entities (streaks, achievements, progress) are always included in full.
    Uses cursor format: "entity_type:index" (e.g., "fish:50").

    Args:
        server_state: Full server state to paginate.
        page_size: Maximum items per page.
        cursor: Previous cursor for continuation, or None for first page.

    Returns:
        Tuple of (paginated_state, has_more, next_cursor).
    """
    # Combine all aquarium-scoped items for pagination
    all_items: list[tuple[str, dict[str, Any]]] = []
    for aquarium in server_state.aquariums:
        all_items.append(("aquarium", aquarium))
    for fish in server_state.fish:
        all_items.append(("fish", fish))
    for event in server_state.events:
        all_items.append(("event", event))
    for schedule in server_state.schedules:
        all_items.append(("schedule", schedule))

    # Determine starting position from cursor
    start_index = 0
    if cursor:
        try:
            start_index = int(cursor)
        except ValueError:
            # Invalid cursor, start from beginning
            start_index = 0

    # Apply pagination
    end_index = start_index + page_size
    paginated_items = all_items[start_index:end_index]
    has_more = end_index < len(all_items)
    next_cursor = str(end_index) if has_more else None

    # Reconstruct paginated server state
    paginated_aquariums: list[dict[str, Any]] = []
    paginated_fish: list[dict[str, Any]] = []
    paginated_events: list[dict[str, Any]] = []
    paginated_schedules: list[dict[str, Any]] = []

    for entity_type, data in paginated_items:
        if entity_type == "aquarium":
            paginated_aquariums.append(data)
        elif entity_type == "fish":
            paginated_fish.append(data)
        elif entity_type == "event":
            paginated_events.append(data)
        elif entity_type == "schedule":
            paginated_schedules.append(data)

    return (
        ServerState(
            aquariums=paginated_aquariums,
            fish=paginated_fish,
            events=paginated_events,
            schedules=paginated_schedules,
            # User-scoped entities are always included in full (not paginated)
            streaks=server_state.streaks,
            achievements=server_state.achievements,
            progress=server_state.progress,
            deleted=server_state.deleted,  # Deleted items are always included in full
        ),
        has_more,
        next_cursor,
    )


async def process_sync(
    db: AsyncSession,
    user_id: UUID,
    request: SyncRequest,
) -> SyncResponse:
    """Process sync request from client.

    Orchestrates the entire sync process:
    1. Validates entity ownership
    2. Applies client changes with conflict resolution
    3. Retrieves server state (delta or full)
    4. Applies pagination to server state
    5. Generates sync token

    All changes are processed in a single transaction with rollback on error.

    Args:
        db: Database session.
        user_id: User ID performing the sync.
        request: Sync request with client changes.

    Returns:
        SyncResponse with server state, conflicts, sync token, and pagination info.

    Raises:
        SyncValidationError: If request validation fails.
        SyncAccessDeniedError: If user doesn't have access to entities.
    """
    logger.info(
        f"Processing sync for user {user_id}: "
        f"{len(request.changes)} changes, last_sync_at={request.last_sync_at}, "
        f"page_size={request.page_size}, cursor={request.cursor}"
    )

    try:
        # Step 1: Validate entity ownership
        await _validate_entity_ownership(db, user_id, request.changes)

        # Step 2: Apply client changes and collect conflicts
        conflicts = await apply_changes(db, user_id, request.changes)

        # Step 2.5: Ensure feeding schedules exist for all aquariums with fish
        await _ensure_schedules_for_user(db, user_id)

        # Step 3: Get server state (delta sync if last_sync_at provided)
        server_state = await get_server_state(db, user_id, request.last_sync_at)

        # Step 4: Apply pagination
        paginated_state, has_more, next_cursor = _apply_pagination(server_state, request.page_size, request.cursor)

        # Step 5: Generate sync token
        sync_token = _generate_sync_token()

        # Commit all changes
        await db.commit()

        logger.info(
            f"Sync completed for user {user_id}: {len(conflicts)} conflicts, has_more={has_more}, token={sync_token}"
        )

        return SyncResponse(
            server_state=paginated_state,
            conflicts=conflicts,
            sync_token=sync_token,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    except (SyncValidationError, SyncAccessDeniedError):
        # Re-raise sync-specific errors
        await db.rollback()
        raise
    except Exception as e:
        # Rollback on any other error
        await db.rollback()
        logger.error(f"Sync failed for user {user_id}: {e}", exc_info=True)
        raise SyncError(f"Sync processing failed: {e}") from e
