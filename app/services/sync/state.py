"""Sync server state retrieval and pagination."""

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.aquarium import Aquarium
from app.models.feeding import FeedingLog, FeedingSchedule
from app.models.fish import Fish
from app.models.gamification import Achievement, Streak, UserProgress
from app.models.user import User
from app.schemas.sync import DeletedEntities, ServerState

from .utils import _entity_to_dict
from .validation import _get_user_aquarium_ids

logger = structlog.get_logger(__name__)


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
    logger.debug("get_server_state for user", user_id=user_id, since=since)

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

    # Query user profile (singleton per user)
    user_profile_data: dict[str, Any] | None = None
    user_stmt = select(User).where(User.id == user_id)
    if since is not None:
        user_stmt = user_stmt.where(User.updated_at >= since)
    user_result = await db.execute(user_stmt)
    user_entity = user_result.scalar_one_or_none()
    if user_entity is not None:
        user_profile_data = _entity_to_dict(user_entity)

    if not user_aquarium_ids:
        logger.debug("No aquariums found for user", user_id=user_id)
        return ServerState(
            aquariums=[],
            fish=[],
            feeding_logs=[],
            schedules=[],
            streaks=streaks_data,
            achievements=achievements_data,
            progress=progress_data,
            user_profile=user_profile_data,
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

    # Query feeding logs (FeedingLog has no soft-delete; use created_at for delta)
    # Eager-load acted_by_user so we can include nickname in the response
    log_stmt = (
        select(FeedingLog)
        .options(joinedload(FeedingLog.acted_by_user))
        .where(FeedingLog.aquarium_id.in_(user_aquarium_ids))
    )

    if since is not None:
        # Delta sync: get logs created after 'since'
        log_stmt = log_stmt.where(FeedingLog.created_at >= since)

    result = await db.execute(log_stmt)
    for log in result.scalars().all():
        events_data.append(_entity_to_dict(log))

    # Query feeding schedules
    schedule_stmt = select(FeedingSchedule).where(FeedingSchedule.aquarium_id.in_(user_aquarium_ids))

    if since is not None:
        # Delta sync: get active schedules updated after 'since'
        active_schedule_stmt = schedule_stmt.where(
            FeedingSchedule.active.is_(True),
            FeedingSchedule.updated_at >= since,
        )
        result = await db.execute(active_schedule_stmt)
        for schedule in result.scalars().all():
            schedules_data.append(_entity_to_dict(schedule))

        # Deactivated schedules after 'since' -> treat as deleted
        inactive_schedule_stmt = schedule_stmt.where(
            FeedingSchedule.active.is_(False),
            FeedingSchedule.updated_at >= since,
        )
        result = await db.execute(inactive_schedule_stmt)
        deleted.schedules = [s.id for s in result.scalars().all()]
    else:
        # Initial sync: get all active schedules
        active_schedule_stmt = schedule_stmt.where(FeedingSchedule.active.is_(True))
        result = await db.execute(active_schedule_stmt)
        for schedule in result.scalars().all():
            schedules_data.append(_entity_to_dict(schedule))

    logger.debug(
        "get_server_state returning",
        aquariums=len(aquariums_data),
        fish=len(fish_data),
        events=len(events_data),
        schedules=len(schedules_data),
        streaks=len(streaks_data),
        achievements=len(achievements_data),
        progress=len(progress_data),
        deleted_aquariums=len(deleted.aquariums),
        deleted_fish=len(deleted.fish),
        deleted_events=len(deleted.feeding_logs),
    )

    return ServerState(
        aquariums=aquariums_data,
        fish=fish_data,
        feeding_logs=events_data,
        schedules=schedules_data,
        streaks=streaks_data,
        achievements=achievements_data,
        progress=progress_data,
        user_profile=user_profile_data,
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
    for log in server_state.feeding_logs:
        all_items.append(("feeding_log", log))
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
    paginated_feeding_logs: list[dict[str, Any]] = []
    paginated_schedules: list[dict[str, Any]] = []

    for entity_type, data in paginated_items:
        if entity_type == "aquarium":
            paginated_aquariums.append(data)
        elif entity_type == "fish":
            paginated_fish.append(data)
        elif entity_type == "feeding_log":
            paginated_feeding_logs.append(data)
        elif entity_type == "schedule":
            paginated_schedules.append(data)

    return (
        ServerState(
            aquariums=paginated_aquariums,
            fish=paginated_fish,
            feeding_logs=paginated_feeding_logs,
            schedules=paginated_schedules,
            # User-scoped entities are always included in full (not paginated)
            streaks=server_state.streaks,
            achievements=server_state.achievements,
            progress=server_state.progress,
            user_profile=server_state.user_profile,
            deleted=server_state.deleted,  # Deleted items are always included in full
        ),
        has_more,
        next_cursor,
    )
