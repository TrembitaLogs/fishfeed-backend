"""Feeding service with business logic for feeding schedules and events."""

import logging
from datetime import UTC, date, datetime, time
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.jobs.notification_jobs import family_feeding_trigger
from app.models.feeding import FeedingEvent, FeedingSchedule
from app.models.fish import Fish
from app.schemas.feeding import ScheduleUpdate
from app.services.aquarium import check_access
from app.services.gamification import check_achievements, update_streak

logger = logging.getLogger(__name__)

# Default scheduled times based on feeding frequency
DEFAULT_TIMES: dict[int, list[str]] = {
    1: ["08:00"],
    2: ["08:00", "20:00"],
    3: ["08:00", "14:00", "20:00"],
}

DEFAULT_FREQUENCY = 2


class FeedingError(Exception):
    """Base exception for feeding errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ScheduleNotFoundError(FeedingError):
    """Raised when feeding schedule is not found."""

    def __init__(self, aquarium_id: UUID):
        super().__init__(f"Feeding schedule not found for aquarium '{aquarium_id}'", status_code=404)


class EventNotFoundError(FeedingError):
    """Raised when feeding event is not found."""

    def __init__(self, event_id: UUID):
        super().__init__(f"Feeding event '{event_id}' not found", status_code=404)


class EventAlreadyCompletedError(FeedingError):
    """Raised when trying to mark an already completed event."""

    def __init__(self, event_id: UUID):
        super().__init__(f"Feeding event '{event_id}' is already completed", status_code=400)


async def generate_schedule(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> FeedingSchedule:
    """Generate or update feeding schedule based on fish species in aquarium.

    Analyzes all fish in the aquarium, selects max(feeding_frequency) among
    all species, and generates evenly distributed scheduled_times.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Returns:
        Created or updated FeedingSchedule.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    await check_access(db, aquarium_id, user_id)

    # Get all active fish with their species
    fish_stmt = (
        select(Fish)
        .where(Fish.aquarium_id == aquarium_id)
        .where(Fish.deleted_at.is_(None))
        .options(selectinload(Fish.species))
    )
    fish_result = await db.execute(fish_stmt)
    fish_list = list(fish_result.scalars().all())

    # Determine feeding frequency and aggregate food types
    if fish_list:
        max_frequency = max(fish.species.feeding_frequency for fish in fish_list)
        # Aggregate unique food types from all species
        all_food_types: set[str] = set()
        for fish in fish_list:
            if fish.species.food_types:
                all_food_types.update(fish.species.food_types)
        food_type = ", ".join(sorted(all_food_types)) if all_food_types else "flakes"
    else:
        # Default schedule when no fish
        max_frequency = DEFAULT_FREQUENCY
        food_type = "flakes"

    # Clamp frequency to 1-3 range
    max_frequency = max(1, min(3, max_frequency))
    scheduled_times = DEFAULT_TIMES[max_frequency]

    # Check for existing schedule
    schedule_stmt = select(FeedingSchedule).where(FeedingSchedule.aquarium_id == aquarium_id)
    schedule_result = await db.execute(schedule_stmt)
    schedule = schedule_result.scalar_one_or_none()

    if schedule:
        # Update existing schedule
        schedule.times_per_day = max_frequency
        schedule.scheduled_times = scheduled_times
        schedule.food_type = food_type
    else:
        # Create new schedule
        schedule = FeedingSchedule(
            aquarium_id=aquarium_id,
            times_per_day=max_frequency,
            scheduled_times=scheduled_times,
            food_type=food_type,
        )
        db.add(schedule)

    await db.flush()
    await db.refresh(schedule)

    # Regenerate today's events
    today = datetime.now(UTC).date()
    await _regenerate_events_for_date(db, schedule, today)

    await db.commit()
    await db.refresh(schedule)

    logger.info(f"Generated schedule for aquarium '{aquarium_id}': {max_frequency}x/day at {scheduled_times}")
    return schedule


async def get_schedule(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> FeedingSchedule | None:
    """Get current feeding schedule for aquarium.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Returns:
        FeedingSchedule if exists, None otherwise.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    await check_access(db, aquarium_id, user_id)

    stmt = select(FeedingSchedule).where(FeedingSchedule.aquarium_id == aquarium_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_schedule(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
    data: ScheduleUpdate,
) -> FeedingSchedule:
    """Manually update feeding schedule.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.
        data: Partial update data.

    Returns:
        Updated FeedingSchedule.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
        ScheduleNotFoundError: If schedule doesn't exist.
    """
    await check_access(db, aquarium_id, user_id)

    stmt = select(FeedingSchedule).where(FeedingSchedule.aquarium_id == aquarium_id)
    result = await db.execute(stmt)
    schedule = result.scalar_one_or_none()

    if schedule is None:
        raise ScheduleNotFoundError(aquarium_id)

    # Apply partial update
    update_data = data.model_dump(exclude_unset=True)

    # Convert time objects to strings for scheduled_times
    if "scheduled_times" in update_data and update_data["scheduled_times"]:
        update_data["scheduled_times"] = [
            t.strftime("%H:%M") if isinstance(t, time) else t for t in update_data["scheduled_times"]
        ]

    for field, value in update_data.items():
        setattr(schedule, field, value)

    await db.flush()

    # Regenerate today's events with new schedule
    today = datetime.now(UTC).date()
    await _regenerate_events_for_date(db, schedule, today)

    await db.commit()
    await db.refresh(schedule)

    logger.info(f"Updated schedule for aquarium '{aquarium_id}': {update_data}")
    return schedule


async def get_all_events(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> list[FeedingEvent]:
    """Get all feeding events for an aquarium.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Returns:
        List of FeedingEvent sorted by scheduled_at DESC.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    await check_access(db, aquarium_id, user_id)

    stmt = (
        select(FeedingEvent)
        .where(FeedingEvent.aquarium_id == aquarium_id)
        .where(FeedingEvent.deleted_at.is_(None))
        .order_by(FeedingEvent.scheduled_at.desc())
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_today_events(
    db: AsyncSession,
    aquarium_id: UUID,
    user_id: UUID,
) -> list[FeedingEvent]:
    """Get feeding events for today.

    Args:
        db: Database session.
        aquarium_id: Aquarium ID.
        user_id: User ID for access check.

    Returns:
        List of FeedingEvent sorted by scheduled_at.

    Raises:
        AquariumNotFoundError: If aquarium not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    await check_access(db, aquarium_id, user_id)

    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, time.min, tzinfo=UTC)
    today_end = datetime.combine(today, time.max, tzinfo=UTC)

    stmt = (
        select(FeedingEvent)
        .where(FeedingEvent.aquarium_id == aquarium_id)
        .where(FeedingEvent.scheduled_at >= today_start)
        .where(FeedingEvent.scheduled_at <= today_end)
        .where(FeedingEvent.deleted_at.is_(None))
        .order_by(FeedingEvent.scheduled_at)
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_event(
    db: AsyncSession,
    event_id: UUID,
    user_id: UUID,
) -> FeedingEvent:
    """Get feeding event by ID with access check.

    Args:
        db: Database session.
        event_id: Event ID.
        user_id: User ID for access check.

    Returns:
        FeedingEvent.

    Raises:
        EventNotFoundError: If event not found or deleted.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    stmt = select(FeedingEvent).where(
        FeedingEvent.id == event_id,
        FeedingEvent.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    event = result.scalar_one_or_none()

    if event is None:
        raise EventNotFoundError(event_id)

    # Check access through aquarium
    await check_access(db, event.aquarium_id, user_id)

    return event


async def mark_as_fed(
    db: AsyncSession,
    event_id: UUID,
    user_id: UUID,
) -> FeedingEvent:
    """Mark feeding event as completed.

    Args:
        db: Database session.
        event_id: Event ID.
        user_id: User ID who completed the feeding.

    Returns:
        Updated FeedingEvent.

    Raises:
        EventNotFoundError: If event not found.
        AquariumAccessDeniedError: If user doesn't have access.
        EventAlreadyCompletedError: If event already completed.
    """
    event = await get_event(db, event_id, user_id)

    if event.status == "completed":
        raise EventAlreadyCompletedError(event_id)

    event.status = "completed"
    event.completed_at = datetime.now(UTC)
    event.completed_by = user_id

    await db.commit()
    await db.refresh(event)

    logger.info(f"Marked event '{event_id}' as fed by user '{user_id}'")

    # Update user streak and check achievements
    try:
        await update_streak(db, user_id)
        await check_achievements(db, user_id)
    except Exception as e:
        logger.error(f"Failed to update streak/achievements for user '{user_id}': {e}")

    # Notify family members about the completed feeding
    try:
        await family_feeding_trigger(db, event_id, user_id)
    except Exception as e:
        # Log but don't fail the feeding completion if notification fails
        logger.error(f"Failed to send family feeding notification: {e}")

    return event


async def mark_as_missed(
    db: AsyncSession,
    event_id: UUID,
) -> FeedingEvent:
    """Mark feeding event as missed.

    Called automatically by background worker when scheduled time passes.

    Args:
        db: Database session.
        event_id: Event ID.

    Returns:
        Updated FeedingEvent.

    Raises:
        EventNotFoundError: If event not found or deleted.
    """
    stmt = select(FeedingEvent).where(
        FeedingEvent.id == event_id,
        FeedingEvent.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    event = result.scalar_one_or_none()

    if event is None:
        raise EventNotFoundError(event_id)

    # Only mark as missed if still pending
    if event.status == "pending":
        event.status = "missed"
        await db.commit()
        await db.refresh(event)
        logger.info(f"Marked event '{event_id}' as missed")

    return event


async def mark_as_missed_by_user(
    db: AsyncSession,
    event_id: UUID,
    user_id: UUID,
) -> FeedingEvent:
    """Mark feeding event as missed by user (manual action).

    Args:
        db: Database session.
        event_id: Event ID.
        user_id: User ID for access check.

    Returns:
        Updated FeedingEvent.

    Raises:
        EventNotFoundError: If event not found.
        AquariumAccessDeniedError: If user doesn't have access.
    """
    event = await get_event(db, event_id, user_id)

    # Only mark as missed if still pending
    if event.status == "pending":
        event.status = "missed"
        await db.commit()
        await db.refresh(event)
        logger.info(f"Marked event '{event_id}' as missed by user '{user_id}'")

    return event


async def create_daily_events(
    db: AsyncSession,
    target_date: date,
) -> int:
    """Create feeding events for all active schedules on specified date.

    Called by background worker to pre-create next day's events.
    Skips creating duplicates if events already exist for the date.

    Args:
        db: Database session.
        target_date: Date to create events for.

    Returns:
        Number of events created.
    """
    # Get all active schedules
    schedules_stmt = select(FeedingSchedule)
    schedules_result = await db.execute(schedules_stmt)
    schedules = list(schedules_result.scalars().all())

    total_created = 0

    for schedule in schedules:
        created = await _create_events_for_schedule(db, schedule, target_date)
        total_created += created

    await db.commit()

    logger.info(f"Created {total_created} feeding events for {target_date}")
    return total_created


async def _regenerate_events_for_date(
    db: AsyncSession,
    schedule: FeedingSchedule,
    target_date: date,
) -> None:
    """Regenerate events for a schedule on specific date.

    Soft deletes existing pending events and creates new ones based on schedule.
    Preserves completed/missed events.

    Args:
        db: Database session.
        schedule: FeedingSchedule to regenerate events for.
        target_date: Date to regenerate events for.
    """
    date_start = datetime.combine(target_date, time.min, tzinfo=UTC)
    date_end = datetime.combine(target_date, time.max, tzinfo=UTC)
    now = datetime.now(UTC)

    # Soft delete only pending, non-deleted events for this schedule on this date
    select_stmt = select(FeedingEvent).where(
        and_(
            FeedingEvent.schedule_id == schedule.id,
            FeedingEvent.scheduled_at >= date_start,
            FeedingEvent.scheduled_at <= date_end,
            FeedingEvent.status == "pending",
            FeedingEvent.deleted_at.is_(None),
        )
    )
    result = await db.execute(select_stmt)
    events_to_delete = result.scalars().all()

    for event in events_to_delete:
        event.deleted_at = now

    # Create new events
    await _create_events_for_schedule(db, schedule, target_date)


async def _create_events_for_schedule(
    db: AsyncSession,
    schedule: FeedingSchedule,
    target_date: date,
) -> int:
    """Create feeding events for a schedule on specific date.

    Creates events per species based on each species' feeding frequency.
    Each species in the aquarium gets events at times appropriate for it.
    Checks for existing events to avoid duplicates.

    Args:
        db: Database session.
        schedule: FeedingSchedule to create events for.
        target_date: Date to create events for.

    Returns:
        Number of events created.
    """
    date_start = datetime.combine(target_date, time.min, tzinfo=UTC)
    date_end = datetime.combine(target_date, time.max, tzinfo=UTC)

    # Get ALL active fish from aquarium with species loaded
    fish_stmt = (
        select(Fish)
        .where(Fish.aquarium_id == schedule.aquarium_id)
        .where(Fish.deleted_at.is_(None))
        .options(selectinload(Fish.species))
    )
    fish_result = await db.execute(fish_stmt)
    all_fish = list(fish_result.scalars().all())

    # Get unique species with their feeding frequencies
    # Use dict to get one fish per species (for species_id tracking)
    species_map: dict[str, Fish] = {}
    for fish in all_fish:
        if fish.species_id not in species_map:
            species_map[fish.species_id] = fish

    # Get existing active events for this schedule on this date
    existing_stmt = (
        select(FeedingEvent)
        .where(FeedingEvent.schedule_id == schedule.id)
        .where(FeedingEvent.scheduled_at >= date_start)
        .where(FeedingEvent.scheduled_at <= date_end)
        .where(FeedingEvent.deleted_at.is_(None))
    )
    existing_result = await db.execute(existing_stmt)
    existing_events = list(existing_result.scalars().all())

    # Track existing (scheduled_at, species_id) pairs to avoid duplicates
    existing_pairs = {(event.scheduled_at, event.species_id) for event in existing_events}

    created_count = 0

    # Create events for each species based on its feeding frequency
    for species_id, fish in species_map.items():
        # Get feeding frequency for this species
        frequency = DEFAULT_FREQUENCY
        if fish.species and fish.species.feeding_frequency:
            frequency = max(1, min(3, fish.species.feeding_frequency))

        # Get scheduled times for this frequency
        species_times = DEFAULT_TIMES.get(frequency, DEFAULT_TIMES[DEFAULT_FREQUENCY])

        for time_str in species_times:
            # Parse time string (format: "HH:MM")
            hour, minute = map(int, time_str.split(":"))
            scheduled_time = time(hour, minute, tzinfo=UTC)
            scheduled_at = datetime.combine(target_date, scheduled_time, tzinfo=UTC)

            # Skip if event already exists for this time + species
            if (scheduled_at, species_id) in existing_pairs:
                continue

            event = FeedingEvent(
                aquarium_id=schedule.aquarium_id,
                schedule_id=schedule.id,
                fish_id=None,  # No longer tied to specific fish
                species_id=species_id,  # Tied to species instead
                scheduled_at=scheduled_at,
                status="pending",
            )
            db.add(event)
            created_count += 1

    # If no fish in aquarium, create events based on schedule's times_per_day
    if not species_map:
        for time_str in schedule.scheduled_times:
            hour, minute = map(int, time_str.split(":"))
            scheduled_time = time(hour, minute, tzinfo=UTC)
            scheduled_at = datetime.combine(target_date, scheduled_time, tzinfo=UTC)

            if (scheduled_at, None) not in existing_pairs:
                event = FeedingEvent(
                    aquarium_id=schedule.aquarium_id,
                    schedule_id=schedule.id,
                    fish_id=None,
                    species_id=None,
                    scheduled_at=scheduled_at,
                    status="pending",
                )
                db.add(event)
                created_count += 1

    return created_count
