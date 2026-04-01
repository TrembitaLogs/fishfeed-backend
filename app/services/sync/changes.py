"""Sync change application: entity-specific change handlers and orchestration."""

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.aquarium import Aquarium, AquariumMember
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak, UserProgress
from app.models.user import User
from app.schemas.sync import ChangeItem, ConflictItem
from app.services import feeding as feeding_service
from app.services.image_service import register_orphaned

from .dead_letter import record_dead_letter
from .utils import RESOLUTION_LABELS, _entity_to_dict, _group_changes_by_entity_type, resolve_conflict

logger = structlog.get_logger(__name__)

VALID_WATER_TYPES = {"freshwater", "saltwater", "brackish"}


def _validate_water_type(value: str | None) -> str | None:
    """Validate and normalize water_type value.

    Args:
        value: Water type string or None.

    Returns:
        Validated water type, falling back to 'freshwater' if invalid.
    """
    if value is None:
        return None
    if value in VALID_WATER_TYPES:
        return value
    logger.warning("Invalid water_type, falling back to 'freshwater'", water_type=value)
    return "freshwater"


# Entity-type-specific regex patterns for photo_key validation.
# Prevents injection and cross-type key substitution through sync payload.
_PHOTO_KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "aquarium": re.compile(r"^aquariums/[0-9a-f-]+/[0-9a-f]+\.webp$"),
    "fish": re.compile(r"^fish/[0-9a-f-]+/[0-9a-f]+\.webp$"),
    "avatar": re.compile(r"^avatars/[0-9a-f-]+/[0-9a-f]+\.webp$"),
}


def _validate_photo_key(photo_key: str | None, entity_type: str) -> bool:
    """Validate photo_key format against entity-type-specific regex.

    None is always valid (means photo deletion).
    Non-null values must match the pattern for the given entity type.

    Args:
        photo_key: S3 object key or None.
        entity_type: One of "aquarium", "fish".

    Returns:
        True if the key is valid or None.
    """
    if photo_key is None:
        return True
    pattern = _PHOTO_KEY_PATTERNS.get(entity_type)
    if pattern is None:
        return False
    return bool(pattern.match(photo_key))


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
            if winner != "client":
                logger.debug("CREATE conflict for aquarium, server wins", entity_id=change.entity_id)
                return ConflictItem(
                    entity_type="aquarium",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution=RESOLUTION_LABELS[winner],
                )
            # Client wins - update existing
            if "name" in change.data:
                existing.name = change.data["name"]
            if "photo_key" in change.data:
                new_key = change.data["photo_key"]
                if _validate_photo_key(new_key, "aquarium"):
                    if existing.photo_key and existing.photo_key != new_key:
                        await register_orphaned(db, existing.photo_key, "aquarium")
                    existing.photo_key = new_key
                else:
                    logger.warning(
                        "invalid_photo_key_ignored",
                        photo_key=new_key,
                        entity_type="aquarium",
                        entity_id=str(change.entity_id),
                    )
            if "water_type" in change.data:
                existing.water_type = _validate_water_type(change.data["water_type"])
            if "capacity" in change.data:
                cap = change.data["capacity"]
                if cap is not None and (not isinstance(cap, (int, float)) or cap <= 0):
                    logger.warning("Invalid capacity, skipping", capacity=cap)
                else:
                    existing.capacity = cap
            logger.debug("CREATE->UPDATE aquarium, client wins", entity_id=change.entity_id)
        else:
            # Create new aquarium
            photo_key = change.data.get("photo_key")
            if photo_key is not None and not _validate_photo_key(photo_key, "aquarium"):
                logger.warning(
                    "invalid_photo_key_ignored",
                    photo_key=photo_key,
                    entity_type="aquarium",
                    entity_id=str(change.entity_id),
                )
                photo_key = None
            # Validate capacity: skip negative/zero values
            capacity = change.data.get("capacity")
            if capacity is not None and (not isinstance(capacity, (int, float)) or capacity <= 0):
                logger.warning("Invalid capacity on create, skipping", capacity=capacity)
                capacity = None

            aquarium = Aquarium(
                id=change.entity_id,
                owner_id=user_id,
                name=change.data.get("name", "Unnamed Aquarium"),
                photo_key=photo_key,
                water_type=_validate_water_type(change.data.get("water_type")),
                capacity=capacity,
            )
            db.add(aquarium)
            # Also add owner as member
            member = AquariumMember(
                aquarium_id=change.entity_id,
                user_id=user_id,
                role="owner",
            )
            db.add(member)
            logger.debug("Created aquarium", entity_id=change.entity_id)

    elif change.operation == "update":
        if existing is None:
            logger.debug("UPDATE skipped for non-existent aquarium", entity_id=change.entity_id)
            return None

        if existing.deleted_at is not None:
            logger.debug("UPDATE skipped for deleted aquarium", entity_id=change.entity_id)
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner != "client":
            logger.debug("UPDATE conflict for aquarium, server wins", entity_id=change.entity_id)
            return ConflictItem(
                entity_type="aquarium",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution=RESOLUTION_LABELS[winner],
            )

        # Client wins - apply update
        if "name" in change.data:
            existing.name = change.data["name"]
        if "photo_key" in change.data:
            new_key = change.data["photo_key"]
            if _validate_photo_key(new_key, "aquarium"):
                if existing.photo_key and existing.photo_key != new_key:
                    await register_orphaned(db, existing.photo_key, "aquarium")
                existing.photo_key = new_key
            else:
                logger.warning(
                    "invalid_photo_key_ignored",
                    photo_key=new_key,
                    entity_type="aquarium",
                    entity_id=str(change.entity_id),
                )
        if "water_type" in change.data:
            existing.water_type = _validate_water_type(change.data["water_type"])
        if "capacity" in change.data:
            cap = change.data["capacity"]
            if cap is not None and (not isinstance(cap, (int, float)) or cap <= 0):
                logger.warning("Invalid capacity, skipping", capacity=cap)
            else:
                existing.capacity = cap
        logger.debug("Updated aquarium", entity_id=change.entity_id)

    elif change.operation == "delete":
        if existing is None:
            logger.debug("DELETE skipped for non-existent aquarium", entity_id=change.entity_id)
            return None

        if existing.deleted_at is not None:
            logger.debug("DELETE skipped for already deleted aquarium", entity_id=change.entity_id)
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner != "client":
            logger.debug("DELETE conflict for aquarium, server wins", entity_id=change.entity_id)
            return ConflictItem(
                entity_type="aquarium",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution=RESOLUTION_LABELS[winner],
            )

        # Client wins - soft delete
        existing.deleted_at = datetime.now(UTC)
        logger.debug("Soft deleted aquarium", entity_id=change.entity_id)

    return None


async def _apply_fish_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
    accessible_aquarium_ids: set[UUID] | None = None,
) -> ConflictItem | None:
    """Apply a single fish change.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.
        accessible_aquarium_ids: Set of aquarium IDs accessible to user (for move validation).

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
            if winner != "client":
                logger.debug("CREATE conflict for fish, server wins", entity_id=change.entity_id)
                return ConflictItem(
                    entity_type="fish",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution=RESOLUTION_LABELS[winner],
                )
            # Client wins - update existing
            if "quantity" in change.data:
                existing.quantity = change.data["quantity"]
            if "custom_name" in change.data:
                existing.custom_name = change.data["custom_name"]
            if "species_id" in change.data:
                existing.species_id = change.data["species_id"]
            if "notes" in change.data:
                notes = change.data["notes"]
                existing.notes = notes[:500] if notes else notes
            if "photo_key" in change.data:
                new_key = change.data["photo_key"]
                if _validate_photo_key(new_key, "fish"):
                    if existing.photo_key and existing.photo_key != new_key:
                        await register_orphaned(db, existing.photo_key, "fish")
                    existing.photo_key = new_key
                else:
                    logger.warning(
                        "invalid_photo_key_ignored",
                        photo_key=new_key,
                        entity_type="fish",
                        entity_id=str(change.entity_id),
                    )
            logger.debug("CREATE->UPDATE fish, client wins", entity_id=change.entity_id)
        else:
            # Create new fish
            aquarium_id = change.data.get("aquarium_id")
            if aquarium_id:
                aquarium_id = UUID(str(aquarium_id))

            photo_key = change.data.get("photo_key")
            if photo_key is not None and not _validate_photo_key(photo_key, "fish"):
                logger.warning(
                    "invalid_photo_key_ignored",
                    photo_key=photo_key,
                    entity_type="fish",
                    entity_id=str(change.entity_id),
                )
                photo_key = None

            notes = change.data.get("notes")

            fish = Fish(
                id=change.entity_id,
                aquarium_id=aquarium_id,
                species_id=change.data.get("species_id", "unknown"),
                quantity=change.data.get("quantity", 1),
                custom_name=change.data.get("custom_name"),
                added_via=change.data.get("added_via", "sync"),
                photo_key=photo_key,
                notes=notes[:500] if notes else notes,
            )
            db.add(fish)
            logger.debug("Created fish", entity_id=change.entity_id)

    elif change.operation == "update":
        if existing is None:
            logger.debug("UPDATE skipped for non-existent fish", entity_id=change.entity_id)
            return None

        if existing.deleted_at is not None:
            logger.debug("UPDATE skipped for deleted fish", entity_id=change.entity_id)
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner != "client":
            logger.debug("UPDATE conflict for fish, server wins", entity_id=change.entity_id)
            return ConflictItem(
                entity_type="fish",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution=RESOLUTION_LABELS[winner],
            )

        # Client wins - apply update
        data = change.data
        if "quantity" in data:
            existing.quantity = data["quantity"]
        if "custom_name" in data:
            existing.custom_name = data["custom_name"]
        if "species_id" in data:
            existing.species_id = data["species_id"]
        if "notes" in data:
            notes = data["notes"]
            existing.notes = notes[:500] if notes else notes
        if "aquarium_id" in data:
            new_aquarium_id = UUID(str(data["aquarium_id"])) if data["aquarium_id"] else None
            if new_aquarium_id and new_aquarium_id != existing.aquarium_id:
                # Validate target aquarium exists, not deleted, and user has access
                target_stmt = select(Aquarium).where(Aquarium.id == new_aquarium_id)
                target_result = await db.execute(target_stmt)
                target_aquarium = target_result.scalar_one_or_none()
                ids_to_check = accessible_aquarium_ids or set()
                if (
                    target_aquarium is not None
                    and target_aquarium.deleted_at is None
                    and new_aquarium_id in ids_to_check
                ):
                    existing.aquarium_id = new_aquarium_id
                    # Atomically update all fish's feeding schedules to new aquarium
                    await db.execute(
                        update(FeedingSchedule)
                        .where(FeedingSchedule.fish_id == existing.id)
                        .values(aquarium_id=new_aquarium_id)
                    )
                else:
                    logger.warning(
                        "fish_move_skipped",
                        fish_id=str(change.entity_id),
                        target_aquarium_id=str(new_aquarium_id),
                        reason="target aquarium not found, deleted, or not accessible",
                    )
        if "photo_key" in data:
            new_key = data["photo_key"]
            if _validate_photo_key(new_key, "fish"):
                if existing.photo_key and existing.photo_key != new_key:
                    await register_orphaned(db, existing.photo_key, "fish")
                existing.photo_key = new_key
            else:
                logger.warning(
                    "invalid_photo_key_ignored",
                    photo_key=new_key,
                    entity_type="fish",
                    entity_id=str(change.entity_id),
                )
        logger.debug("Updated fish", entity_id=change.entity_id)

    elif change.operation == "delete":
        if existing is None:
            logger.debug("DELETE skipped for non-existent fish", entity_id=change.entity_id)
            return None

        if existing.deleted_at is not None:
            logger.debug("DELETE skipped for already deleted fish", entity_id=change.entity_id)
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner != "client":
            logger.debug("DELETE conflict for fish, server wins", entity_id=change.entity_id)
            return ConflictItem(
                entity_type="fish",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution=RESOLUTION_LABELS[winner],
            )

        # Client wins - soft delete
        now = datetime.now(UTC)
        existing.deleted_at = now

        # Deactivate all schedules for this fish
        deactivate_stmt = (
            update(FeedingSchedule)
            .where(FeedingSchedule.fish_id == change.entity_id)
            .values(active=False)
        )
        await db.execute(deactivate_stmt)
        logger.debug("Soft deleted fish and deactivated its schedules", entity_id=change.entity_id)

    return None


async def _get_user_nickname(db: AsyncSession, user_id: UUID) -> str | None:
    """Get user nickname by ID for conflict reporting."""
    stmt = select(User.nickname, User.email).where(User.id == user_id)
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    return str(row.nickname or row.email)


async def _apply_feeding_log_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single feeding log change using first-write-wins.

    FeedingLog is an immutable fact record. Duplicates are detected via
    UNIQUE(schedule_id, scheduled_for) constraint. The first log to reach
    the server wins; subsequent attempts return a conflict.

    Only CREATE is supported. UPDATE and DELETE are ignored since logs
    are immutable records of feeding actions.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if duplicate detected, None otherwise.
    """
    if change.operation == "create":
        # Check if log with same ID already exists
        stmt = select(FeedingLog).where(FeedingLog.id == change.entity_id)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            # First-write-wins: existing log stays, return conflict
            logger.debug("CREATE conflict for feeding_log, already exists", entity_id=change.entity_id)
            server_data = _entity_to_dict(existing)
            server_data["acted_by_user_name"] = await _get_user_nickname(db, existing.acted_by_user_id)
            return ConflictItem(
                entity_type="feeding_log",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=server_data,
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.created_at,
                resolution="server_wins",
            )

        # Parse required fields
        aquarium_id = change.data.get("aquarium_id")
        if aquarium_id:
            aquarium_id = UUID(str(aquarium_id))

        schedule_id = change.data.get("schedule_id")
        if schedule_id:
            schedule_id = UUID(str(schedule_id))

        fish_id = change.data.get("fish_id")
        if fish_id:
            fish_id = UUID(str(fish_id))

        scheduled_for_str = change.data.get("scheduled_for")
        if isinstance(scheduled_for_str, str):
            scheduled_for = datetime.fromisoformat(scheduled_for_str.replace("Z", "+00:00")).replace(tzinfo=None)
        else:
            scheduled_for = scheduled_for_str or datetime.now(UTC).replace(tzinfo=None)
        if hasattr(scheduled_for, "tzinfo") and scheduled_for.tzinfo is not None:
            scheduled_for = scheduled_for.replace(tzinfo=None)

        acted_at_str = change.data.get("acted_at")
        if acted_at_str and isinstance(acted_at_str, str):
            acted_at = datetime.fromisoformat(acted_at_str.replace("Z", "+00:00"))
        elif acted_at_str:
            acted_at = acted_at_str
        else:
            acted_at = datetime.now(UTC)

        acted_by_user_id = change.data.get("acted_by_user_id")
        if acted_by_user_id:
            acted_by_user_id = UUID(str(acted_by_user_id))
        else:
            acted_by_user_id = user_id

        device_id = change.data.get("device_id")
        if device_id:
            device_id = UUID(str(device_id))
        else:
            device_id = uuid4()

        action = change.data.get("action", "fed")
        notes = change.data.get("notes")

        # Check for UNIQUE(schedule_id, scheduled_for) conflict before insert
        if schedule_id and scheduled_for:
            dup_stmt = select(FeedingLog).where(
                and_(
                    FeedingLog.schedule_id == schedule_id,
                    FeedingLog.scheduled_for == scheduled_for,
                )
            )
            dup_result = await db.execute(dup_stmt)
            existing_dup = dup_result.scalar_one_or_none()

            if existing_dup is not None:
                # First-write-wins: existing log stays
                logger.info(
                    "Duplicate feeding_log, existing wins",
                    schedule_id=schedule_id,
                    scheduled_for=scheduled_for,
                    existing_id=existing_dup.id,
                )
                dup_server_data = _entity_to_dict(existing_dup)
                dup_server_data["acted_by_user_name"] = await _get_user_nickname(db, existing_dup.acted_by_user_id)
                return ConflictItem(
                    entity_type="feeding_log",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=dup_server_data,
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing_dup.created_at,
                    resolution="server_wins",
                )

        log = FeedingLog(
            id=change.entity_id,
            schedule_id=schedule_id,
            fish_id=fish_id,
            aquarium_id=aquarium_id,
            scheduled_for=scheduled_for,
            action=action,
            acted_at=acted_at,
            acted_by_user_id=acted_by_user_id,
            device_id=device_id,
            notes=notes,
        )
        db.add(log)
        logger.debug("Created feeding_log", entity_id=change.entity_id)

    elif change.operation in ("update", "delete"):
        # FeedingLog records are immutable - ignore update/delete
        logger.debug("Operation ignored for immutable feeding_log", operation=change.operation.upper(), entity_id=change.entity_id)

    return None


def _apply_schedule_fields(
    schedule: FeedingSchedule,
    data: dict[str, Any],
    user_id: UUID,
) -> None:
    """Apply updatable fields from change data to a FeedingSchedule.

    Args:
        schedule: Existing schedule entity to update.
        data: Dictionary of field values from client.
        user_id: User ID performing the change.
    """
    from datetime import date as dt_date
    from datetime import time as dt_time

    if "fish_id" in data and data["fish_id"]:
        schedule.fish_id = UUID(str(data["fish_id"]))
    if "time" in data:
        time_str = data["time"]
        if isinstance(time_str, str):
            parts = time_str.split(":")
            schedule.time = dt_time(int(parts[0]), int(parts[1]))
        else:
            schedule.time = time_str
    if "interval_days" in data:
        schedule.interval_days = data["interval_days"]
    if "anchor_date" in data:
        ad = data["anchor_date"]
        if isinstance(ad, str):
            schedule.anchor_date = dt_date.fromisoformat(ad[:10])
        else:
            schedule.anchor_date = ad
    if "food_type" in data:
        schedule.food_type = data["food_type"]
    if "portion_hint" in data:
        schedule.portion_hint = data["portion_hint"]
    if "active" in data:
        schedule.active = data["active"]
    if "created_by_user_id" in data and data["created_by_user_id"]:
        schedule.created_by_user_id = UUID(str(data["created_by_user_id"]))


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
            if winner != "client":
                logger.debug("CREATE conflict for schedule, server wins", entity_id=change.entity_id)
                return ConflictItem(
                    entity_type="schedule",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution=RESOLUTION_LABELS[winner],
                )
            # Client wins - update existing
            _apply_schedule_fields(existing, change.data, user_id)
            logger.debug("CREATE->UPDATE schedule, client wins", entity_id=change.entity_id)
        else:
            # Create new schedule
            aquarium_id = change.data.get("aquarium_id")
            if aquarium_id:
                aquarium_id = UUID(str(aquarium_id))

            fish_id = change.data.get("fish_id")
            if fish_id:
                fish_id = UUID(str(fish_id))

            from datetime import date as dt_date
            from datetime import time as dt_time

            time_str = change.data.get("time", "09:00")
            if isinstance(time_str, str):
                parts = time_str.split(":")
                schedule_time = dt_time(int(parts[0]), int(parts[1]))
            else:
                schedule_time = time_str

            anchor_date_val = change.data.get("anchor_date")
            if isinstance(anchor_date_val, str):
                anchor_date = dt_date.fromisoformat(anchor_date_val[:10])
            elif anchor_date_val:
                anchor_date = anchor_date_val
            else:
                anchor_date = datetime.now(UTC).date()

            schedule = FeedingSchedule(
                id=change.entity_id,
                aquarium_id=aquarium_id,
                fish_id=fish_id,
                time=schedule_time,
                interval_days=change.data.get("interval_days", 1),
                anchor_date=anchor_date,
                food_type=change.data.get("food_type", "flakes"),
                portion_hint=change.data.get("portion_hint"),
                active=change.data.get("active", True),
                created_by_user_id=(
                    UUID(str(change.data["created_by_user_id"]))
                    if change.data.get("created_by_user_id")
                    else user_id
                ),
            )
            db.add(schedule)
            logger.debug("Created schedule", entity_id=change.entity_id)

    elif change.operation == "update":
        if existing is None:
            logger.debug("UPDATE skipped for non-existent schedule", entity_id=change.entity_id)
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner != "client":
            logger.debug("UPDATE conflict for schedule, server wins", entity_id=change.entity_id)
            return ConflictItem(
                entity_type="schedule",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution=RESOLUTION_LABELS[winner],
            )

        # Client wins - apply update
        _apply_schedule_fields(existing, change.data, user_id)
        logger.debug("Updated schedule", entity_id=change.entity_id)

    elif change.operation == "delete":
        if existing is None:
            logger.debug("DELETE skipped for non-existent schedule", entity_id=change.entity_id)
            return None

        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner != "client":
            logger.debug("DELETE conflict for schedule, server wins", entity_id=change.entity_id)
            return ConflictItem(
                entity_type="schedule",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution=RESOLUTION_LABELS[winner],
            )

        # Client wins - hard delete
        await db.delete(existing)
        logger.debug("Hard deleted schedule", entity_id=change.entity_id)

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
            logger.debug("Created streak for user", user_id=user_id)
        else:
            # Check for conflict
            winner = resolve_conflict(existing.updated_at, change.client_updated_at)
            if winner != "client":
                logger.debug("UPDATE conflict for streak, server wins", user_id=user_id)
                return ConflictItem(
                    entity_type="streak",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution=RESOLUTION_LABELS[winner],
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
            logger.debug("Updated streak for user", user_id=user_id)

    # Delete operation not supported for streaks
    elif change.operation == "delete":
        logger.debug("DELETE not supported for streaks, ignoring", entity_id=change.entity_id)

    return None


async def _apply_achievement_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a single achievement change from mobile client.

    Mobile is the source of truth for achievements. Backend just stores them.
    Lookup is by (user_id, achievement_type) to ensure idempotency regardless
    of the entity_id format mobile sends.

    Args:
        db: Database session.
        user_id: User ID applying the change.
        change: Change item to apply.

    Returns:
        ConflictItem if conflict detected, None otherwise.
    """
    achievement_type = change.data.get("achievement_type", "unknown")

    # Look up by (user_id, achievement_type) for idempotency
    stmt = select(Achievement).where(
        and_(
            Achievement.user_id == user_id,
            Achievement.achievement_type == achievement_type,
        )
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if change.operation == "create":
        if existing is not None:
            logger.debug("Achievement already exists for user, skipping", achievement_type=achievement_type, user_id=user_id)
            return None

        unlocked_at = None
        if "unlocked_at" in change.data and change.data["unlocked_at"]:
            ua = change.data["unlocked_at"]
            if isinstance(ua, str):
                unlocked_at = datetime.fromisoformat(ua.replace("Z", "+00:00"))
            else:
                unlocked_at = ua

        achievement = Achievement(
            id=uuid4(),
            user_id=user_id,
            achievement_type=achievement_type,
            unlocked_at=unlocked_at or datetime.now(UTC),
        )
        db.add(achievement)
        logger.debug("Created achievement for user", achievement_type=achievement_type, user_id=user_id)

    elif change.operation == "update":
        if existing is None:
            logger.debug("UPDATE skipped for non-existent achievement", achievement_type=achievement_type)
            return None

        if "shared_at" in change.data:
            sa = change.data["shared_at"]
            if sa:
                if isinstance(sa, str):
                    existing.shared_at = datetime.fromisoformat(sa.replace("Z", "+00:00"))
                else:
                    existing.shared_at = sa
            else:
                existing.shared_at = None
        logger.debug("Updated achievement for user", achievement_type=achievement_type, user_id=user_id)

    elif change.operation == "delete":
        logger.debug("DELETE not supported for achievements, ignoring")

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
            logger.debug("Created progress for user", user_id=user_id)
        else:
            # Check for conflict
            winner = resolve_conflict(existing.updated_at, change.client_updated_at)
            if winner != "client":
                logger.debug("UPDATE conflict for progress, server wins", user_id=user_id)
                return ConflictItem(
                    entity_type="progress",
                    entity_id=change.entity_id,
                    client_data=change.data,
                    server_data=_entity_to_dict(existing),
                    client_updated_at=change.client_updated_at,
                    server_updated_at=existing.updated_at,
                    resolution=RESOLUTION_LABELS[winner],
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
            logger.debug("Updated progress for user", user_id=user_id)

    # Delete operation not supported for progress
    elif change.operation == "delete":
        logger.debug("DELETE not supported for progress, ignoring", entity_id=change.entity_id)

    return None


async def _apply_user_profile_change(
    db: AsyncSession,
    user_id: UUID,
    change: ChangeItem,
) -> ConflictItem | None:
    """Apply a user profile change. Only update operation is meaningful."""
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is None:
        logger.debug("User not found, skipping profile change", user_id=user_id)
        return None

    if change.operation in ("create", "update"):
        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner != "client":
            logger.debug("UPDATE conflict for user_profile, server wins", user_id=user_id)
            return ConflictItem(
                entity_type="user_profile",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution=RESOLUTION_LABELS[winner],
            )
        # Client wins - apply update
        if "nickname" in change.data:
            existing.nickname = change.data["nickname"]
        if "avatar_key" in change.data:
            new_key = change.data["avatar_key"]
            if _validate_photo_key(new_key, "avatar"):
                if existing.avatar_key and existing.avatar_key != new_key:
                    await register_orphaned(db, existing.avatar_key, "avatar")
                existing.avatar_key = new_key
            else:
                logger.warning(
                    "invalid_avatar_key_ignored",
                    avatar_key=new_key,
                    entity_type="avatar",
                    entity_id=str(user_id),
                )
        if "settings" in change.data:
            existing.settings = change.data["settings"]
        logger.debug("Updated user_profile for user", user_id=user_id)

    elif change.operation == "delete":
        logger.debug("DELETE not supported for user_profile, ignoring", entity_id=change.entity_id)

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
        "Applying changes for user",
        user_id=user_id,
        aquariums=len(grouped["aquarium"]),
        fish=len(grouped["fish"]),
        feeding_logs=len(grouped["feeding_log"]),
        schedules=len(grouped["schedule"]),
        streaks=len(grouped["streak"]),
        achievements=len(grouped["achievement"]),
        progress=len(grouped["progress"]),
        user_profile=len(grouped["user_profile"]),
    )

    async def _safe_apply(
        change: ChangeItem,
        handler: Any,
        *args: Any,
    ) -> ConflictItem | None:
        """Apply a change handler, recording failures to dead-letter instead of aborting sync."""
        try:
            result: ConflictItem | None = await handler(db, user_id, change, *args)
            return result
        except Exception as e:
            logger.error(
                "Change handler failed, recording to dead letter",
                entity_type=change.entity_type,
                entity_id=str(change.entity_id),
                operation=change.operation,
                error=str(e),
            )
            await record_dead_letter(db, user_id, change, e)
            return None

    # Process aquarium changes
    for change in grouped["aquarium"]:
        conflict = await _safe_apply(change, _apply_aquarium_change)
        if conflict:
            conflicts.append(conflict)

    # Fetch accessible aquarium IDs for fish move validation
    accessible_aquarium_ids: set[UUID] | None = None
    if grouped["fish"]:
        from .validation import _get_user_aquarium_ids

        accessible_aquarium_ids = await _get_user_aquarium_ids(db, user_id)

    # Process fish changes and collect affected aquarium IDs
    affected_aquarium_ids: set[UUID] = set()
    for change in grouped["fish"]:
        conflict = await _safe_apply(change, _apply_fish_change, accessible_aquarium_ids)
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

    # Process feeding log changes
    for change in grouped["feeding_log"]:
        conflict = await _safe_apply(change, _apply_feeding_log_change)
        if conflict:
            conflicts.append(conflict)

    # Process schedule changes
    for change in grouped["schedule"]:
        conflict = await _safe_apply(change, _apply_schedule_change)
        if conflict:
            conflicts.append(conflict)

    # Process streak changes (user-scoped)
    for change in grouped["streak"]:
        conflict = await _safe_apply(change, _apply_streak_change)
        if conflict:
            conflicts.append(conflict)

    # Process achievement changes (user-scoped)
    for change in grouped["achievement"]:
        conflict = await _safe_apply(change, _apply_achievement_change)
        if conflict:
            conflicts.append(conflict)

    # Process progress changes (user-scoped)
    for change in grouped["progress"]:
        conflict = await _safe_apply(change, _apply_progress_change)
        if conflict:
            conflicts.append(conflict)

    # Process user_profile changes (user-scoped)
    for change in grouped["user_profile"]:
        conflict = await _safe_apply(change, _apply_user_profile_change)
        if conflict:
            conflicts.append(conflict)

    # Flush to ensure all changes are applied
    await db.flush()

    # NOTE: Removed auto-generation of feeding schedules here.
    # Schedule creation is client-initiated (offline-first architecture).
    # _ensure_schedules_for_user() provides fallback for new fish without schedules.

    logger.debug("Applied changes", conflict_count=len(conflicts))
    return conflicts


async def _ensure_schedules_for_user(
    db: AsyncSession,
    user_id: UUID,
) -> None:
    """Ensure feeding schedules exist for all user's fish.

    Checks each fish owned by user. If a fish has no schedule, generates
    schedules based on species feeding requirements. Works per-fish, not
    per-aquarium, so new fish added to existing aquariums get their own schedules.

    Args:
        db: Database session.
        user_id: User ID.
    """
    from .validation import _get_user_aquarium_ids

    # Get all user's aquarium IDs
    user_aquarium_ids = await _get_user_aquarium_ids(db, user_id)
    if not user_aquarium_ids:
        return

    # Get all active fish in user's aquariums
    all_fish_stmt = (
        select(Fish.id, Fish.aquarium_id)
        .where(Fish.aquarium_id.in_(user_aquarium_ids))
        .where(Fish.deleted_at.is_(None))
    )
    result = await db.execute(all_fish_stmt)
    all_fish = {row.id: row.aquarium_id for row in result}

    if not all_fish:
        return

    # Get fish that already have schedules
    fish_with_schedule_stmt = (
        select(FeedingSchedule.fish_id)
        .where(FeedingSchedule.fish_id.in_(all_fish.keys()))
        .distinct()
    )
    result = await db.execute(fish_with_schedule_stmt)
    fish_with_schedule = set(result.scalars().all())

    # Find fish that need schedules
    fish_needing_schedule = set(all_fish.keys()) - fish_with_schedule

    if not fish_needing_schedule:
        return

    # Group by aquarium for efficient schedule generation
    aquariums_needing_schedule = {all_fish[fish_id] for fish_id in fish_needing_schedule}

    logger.info(
        f"Creating schedules for {len(fish_needing_schedule)} fish "
        f"in {len(aquariums_needing_schedule)} aquariums"
    )

    # generate_schedule() is idempotent — it skips fish that already have schedules
    for aquarium_id in aquariums_needing_schedule:
        try:
            await feeding_service.generate_schedule(db, aquarium_id, user_id)
            logger.debug("Generated schedules for aquarium", aquarium_id=aquarium_id)
        except Exception as e:
            logger.warning("Failed to generate schedules for aquarium", aquarium_id=aquarium_id, error=str(e))
