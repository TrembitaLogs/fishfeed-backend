"""Sync change application: entity-specific change handlers and orchestration."""

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

from .utils import _entity_to_dict, _group_changes_by_entity_type, resolve_conflict

logger = structlog.get_logger(__name__)


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

        # Deactivate all schedules for this fish
        deactivate_stmt = (
            update(FeedingSchedule)
            .where(FeedingSchedule.fish_id == change.entity_id)
            .values(active=False)
        )
        await db.execute(deactivate_stmt)
        logger.debug(f"Soft deleted fish {change.entity_id} and deactivated its schedules")

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
            logger.debug(f"CREATE conflict for feeding_log {change.entity_id}: already exists")
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
                    f"Duplicate feeding_log for schedule {schedule_id} "
                    f"at {scheduled_for}: existing {existing_dup.id} wins"
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
        logger.debug(f"Created feeding_log {change.entity_id}")

    elif change.operation in ("update", "delete"):
        # FeedingLog records are immutable - ignore update/delete
        logger.debug(f"{change.operation.upper()} ignored for immutable feeding_log {change.entity_id}")

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
            _apply_schedule_fields(existing, change.data, user_id)
            logger.debug(f"CREATE->UPDATE schedule {change.entity_id}: client wins")
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
                anchor_date = dt_date.today()

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
        _apply_schedule_fields(existing, change.data, user_id)
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
        logger.debug(f"User {user_id} not found, skipping profile change")
        return None

    if change.operation in ("create", "update"):
        winner = resolve_conflict(existing.updated_at, change.client_updated_at)
        if winner == "server":
            logger.debug(f"UPDATE conflict for user_profile {user_id}: server wins")
            return ConflictItem(
                entity_type="user_profile",
                entity_id=change.entity_id,
                client_data=change.data,
                server_data=_entity_to_dict(existing),
                client_updated_at=change.client_updated_at,
                server_updated_at=existing.updated_at,
                resolution="server_wins",
            )
        # Client wins - apply update
        if "nickname" in change.data:
            existing.nickname = change.data["nickname"]
        if "avatar_url" in change.data:
            existing.avatar_url = change.data["avatar_url"]
        if "settings" in change.data:
            existing.settings = change.data["settings"]
        logger.debug(f"Updated user_profile for user {user_id}")

    elif change.operation == "delete":
        logger.debug(f"DELETE not supported for user_profile, ignoring {change.entity_id}")

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
        f"{len(grouped['feeding_log'])} feeding_logs, "
        f"{len(grouped['schedule'])} schedules, "
        f"{len(grouped['streak'])} streaks, "
        f"{len(grouped['achievement'])} achievements, "
        f"{len(grouped['progress'])} progress, "
        f"{len(grouped['user_profile'])} user_profile"
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

    # Process feeding log changes
    for change in grouped["feeding_log"]:
        conflict = await _apply_feeding_log_change(db, user_id, change)
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

    # Process user_profile changes (user-scoped)
    for change in grouped["user_profile"]:
        conflict = await _apply_user_profile_change(db, user_id, change)
        if conflict:
            conflicts.append(conflict)

    # Flush to ensure all changes are applied
    await db.flush()

    # NOTE: Removed auto-generation of feeding schedules here.
    # Schedule creation is client-initiated (offline-first architecture).
    # _ensure_schedules_for_user() provides fallback for new fish without schedules.

    logger.debug(f"Applied changes with {len(conflicts)} conflicts")
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
            logger.debug(f"Generated schedules for aquarium {aquarium_id}")
        except Exception as e:
            logger.warning(f"Failed to generate schedules for aquarium {aquarium_id}: {e}")
